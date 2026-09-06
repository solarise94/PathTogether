# -*- coding: utf-8 -*-
"""瓦片内存缓存：数量 + 字节双预算 LRU、进程内 single-flight、受限指标。

image-transport-upgrade §6：
- 保留 per-process LRU；新增 ``TILE_CACHE_MAX_BYTES``（默认 192 MiB/worker），
  与既有 ``TILE_CACHE_MAX`` 数量上限任一先到即淘汰；不沿用"3000 张约 180MB"
  估算作为内存硬上限（q95/4:4:4 与 4:2:0 瓦片体积差异明显）。
- 覆盖同 key 更新（字节账本按差额记账）、单条超预算不缓存、TTL 过期、清理。
- 同一精确 key 的并发 miss 做 single-flight：锁内不做 I/O/编码；失败/异常
  清理 in-flight 状态，等待者有界退出，不留悬挂 Future；不同 key 不串行。
- 指标：hit/miss、bytes/evictions、single-flight joins/timeouts、encode/decode
  耗时（按 profile 标签，基数受限）；不记录图像名、token、坐标。

app.py 与 share_server.py 各自实例化（进程独立，跨 worker 不共享——§6.5）。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

#: 等待者的有界退出窗口（秒）。leader 以 try/finally 保证完成信号，本上限只
#: 兜底线程被杀等病态场景；超时抛 SingleFlightTimeout（路由映射 503 可重试）。
SINGLE_FLIGHT_WAIT_TIMEOUT = 120.0

#: encode/decode 耗时指标的最大标签数（profile 白名单 + legacy，防基数膨胀）
_MAX_TIMER_LABELS = 16


class SingleFlightTimeout(Exception):
    """等待者超过有界窗口未得到 leader 结果（可重试）。"""


class _Flight:
    __slots__ = ("event", "result", "exception")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.exception = None


class TileMemoryCache:
    """线程安全 LRU（数量 + 字节双预算）。value 必须是 bytes。"""

    def __init__(self, max_entries=3000, max_bytes=192 * 1024 * 1024,
                 sizer=len):
        """``sizer(value) -> int``：字节计账函数（share 端 TTL 包装值
        ``(expires_at, data)`` 传 ``lambda v: len(v[1])``）。"""
        self._sizer = sizer
        self._max_entries = max(0, int(max_entries))
        self._max_bytes = max(0, int(max_bytes))
        self._data = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        # 指标（进程内累计；snapshot 读时在锁内取一致性视图）
        self._hits = 0
        self._misses = 0
        self._evictions_entries = 0
        self._evicted_bytes = 0
        self._rejected_oversize = 0

    # ---- 基本操作 ----
    def get(self, key):
        with self._lock:
            data = self._data.get(key)
            if data is not None:
                self._data.move_to_end(key)
                self._hits += 1
            else:
                self._misses += 1
            return data

    def put(self, key, data):
        """写入并按双预算淘汰；同 key 覆盖按差额记账；单条超预算不缓存。"""
        size = self._sizer(data)
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= self._sizer(old)
            if size > self._max_bytes or self._max_entries <= 0 \
                    or self._max_bytes <= 0:
                self._rejected_oversize += 1
                return
            self._data[key] = data
            self._bytes += size
            while self._data and (
                    len(self._data) > self._max_entries
                    or self._bytes > self._max_bytes):
                _, evicted = self._data.popitem(last=False)
                self._bytes -= self._sizer(evicted)
                self._evictions_entries += 1
                self._evicted_bytes += self._sizer(evicted)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def keys(self):
        """当前全部键的快照（测试/清理辅助；不保证读取期间的一致性）。"""
        with self._lock:
            return list(self._data.keys())

    # ---- 指标 ----
    def stats(self):
        with self._lock:
            return {
                "entries": len(self._data),
                "bytes": self._bytes,
                "max_entries": self._max_entries,
                "max_bytes": self._max_bytes,
                "hits": self._hits,
                "misses": self._misses,
                "evictions_entries": self._evictions_entries,
                "evicted_bytes": self._evicted_bytes,
                "rejected_oversize": self._rejected_oversize,
            }


class SingleFlight:
    """同一精确 key 的并发 miss 合并；不同 key 完全并行（§6.4）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}
        self._joins = 0
        self._timeouts = 0

    def run(self, key, fn):
        """leader 执行 fn()；等待者阻塞到同一结果/异常（有界）。

        锁只保护登记簿，fn() 在锁外执行；fn 异常会传给 leader 调用方与所有
        等待者，in-flight 状态由 finally 清理。
        """
        with self._lock:
            flight = self._inflight.get(key)
            if flight is not None:
                self._joins += 1
                leader = False
            else:
                flight = _Flight()
                self._inflight[key] = flight
                leader = True
        if not leader:
            if not flight.event.wait(SINGLE_FLIGHT_WAIT_TIMEOUT):
                with self._lock:
                    self._timeouts += 1
                raise SingleFlightTimeout("viewer tile single-flight 等待超时")
            if flight.exception is not None:
                raise flight.exception
            return flight.result
        try:
            result = fn()
        except BaseException as e:  # noqa: BLE001  异常也要传给等待者
            flight.exception = e
            raise
        else:
            flight.result = result
            return result
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            flight.event.set()

    def stats(self):
        with self._lock:
            return {"joins": self._joins, "timeouts": self._timeouts,
                    "inflight": len(self._inflight)}


class ViewerMetrics:
    """受限基数的过程指标（§6.6）：profile/mode/状态码枚举标签。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._enc_ms = {}     # profile_id -> [count, total_ms]
        self._dec_count = 0
        self._dec_ms = 0.0
        self._resp = {}       # (mode, profile) -> [count, bytes]
        self._status = {}     # "200"/"304"/"409"/"400"/"4xx"/"5xx" -> count

    def observe_encode(self, profile_id, ms):
        with self._lock:
            slot = self._enc_ms.setdefault(str(profile_id), [0, 0.0])
            slot[0] += 1
            slot[1] += float(ms)

    def observe_decode(self, ms):
        with self._lock:
            self._dec_count += 1
            self._dec_ms += float(ms)

    def observe_response(self, mode, profile_id, nbytes):
        key = (str(mode)[:16], str(profile_id)[:32])
        with self._lock:
            slot = self._resp.setdefault(key, [0, 0])
            slot[0] += 1
            slot[1] += int(nbytes)

    def observe_status(self, bucket):
        with self._lock:
            self._status[str(bucket)[:8]] = \
                self._status.get(str(bucket)[:8], 0) + 1

    def snapshot(self):
        with self._lock:
            return {
                "encode": {k: {"count": v[0], "total_ms": round(v[1], 3)}
                           for k, v in self._enc_ms.items()},
                "decode": {"count": self._dec_count,
                           "total_ms": round(self._dec_ms, 3)},
                "responses": {"%s|%s" % k: {"count": v[0], "bytes": v[1]}
                              for k, v in self._resp.items()},
                "status": dict(self._status),
            }
