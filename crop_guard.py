# -*- coding: utf-8 -*-
"""crop 像素闸（P0-A §3.5，docs/open-registration-security-remediation.md）。

主站 ``/api/slide/<name>/crop`` 与分享端 ``/s/<token>/api/slide/<name>/crop``
共用的防护模块——两处此前各有一份几乎相同的 crop 逻辑（最大 40000×40000
level-0 区域再编码 PNG，仅像素缓冲即 ~4.8 GB），且没有任何闸。本模块把
三道闸收敛到一处，两端 import 同一实现，避免逻辑漂移：

1. **单请求像素硬闸（``CROP_MAX_PIXELS``）**：按实际 clamp 后的
   width×height 判定，超限在任何解码（read_region）前拒绝，返回稳定
   413 ``crop_too_large``。默认 4096²=16777216，与 Plugin region 的
   ``PLUGIN_REGION_MAX_PIXELS`` 同值（docs §3.5 明确沿用），配置名独立。
2. **每分钟像素预算（``CROP_PIXEL_BUDGET_PER_MIN``）**：60s 滑动窗口，
   主站按 user_id、分享端按 share token 计；超限 429 ``crop_rate_limited``。
3. **并发闸（``CROP_MAX_CONCURRENT``）**：进程级信号量，超载 429
   ``crop_busy``。

**进程内实现的理由**（docs §3.5 允许「进程内+说明理由」）：crop 的真实
成本是 worker 内的解码与 PNG 编码（CPU/内存），威胁是单个 worker 被
拖死，而非跨 worker 的配额公平性；本仓库 Plugin region 三道闸
（app.py Stage 4-2）基于同样理由选择进程内。gunicorn 2 workers ×
8 threads 的部署下，进程内并发闸已把单 worker 的同时在途 crop 数压到
常数级；若未来多 worker 部署需要全局公平，可升级为 PG 权威预算（本
模块的 admit 接口保持不变，路由层无需改动）。重启清零可接受（预算
窗口仅 60s）。

默认值依据：
  - CROP_MAX_PIXELS=4096²：与 Plugin region 上限一致（16.7M 像素 ≈
    RGB 48 MiB 解码缓冲），病理视野导出够用；
  - CROP_PIXEL_BUDGET_PER_MIN=16×4096²：与 Plugin region 预算同量级
    （每分钟 16 次满额 crop）；
  - CROP_MAX_CONCURRENT=4：与 Plugin region 并发一致，低于 gunicorn
    单 worker 线程数（8），保证普通 API 线程余量。
  均为 env 可调；[测] 上线前按真实切片尺寸分布与并发压测复核
  （4096² 对超大视野拼接导出是否够，需按用户实际使用 P95 调整）。
"""

import math
import os
import threading
import time
from collections import deque


def _env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


#: read_region 前的单请求像素硬上限（clamp 后实际 width×height）。
CROP_MAX_PIXELS = _env_int("CROP_MAX_PIXELS", 4096 ** 2)

#: 每 subject 每分钟像素预算（60s 滑动窗口）。
CROP_PIXEL_BUDGET_PER_MIN = _env_int("CROP_PIXEL_BUDGET_PER_MIN",
                                     16 * 4096 ** 2)

#: 进程级并发上限。
CROP_MAX_CONCURRENT = max(1, _env_int("CROP_MAX_CONCURRENT", 4))


class CropTooLargeError(Exception):
    """clamp 后像素超过 CROP_MAX_PIXELS（任何解码前拒绝）。"""

    code = "crop_too_large"


class CropRateLimitedError(Exception):
    """每分钟像素预算超限。携带 retry_after 秒。"""

    code = "crop_rate_limited"

    def __init__(self, message=None, retry_after=1):
        self.retry_after = int(max(1, retry_after))
        super().__init__(message or "crop 请求过于频繁，请稍后重试")


class CropBusyError(Exception):
    """并发闸已满。"""

    code = "crop_busy"


def check_pixel_limit(width, height, max_pixels=None):
    """像素硬闸：clamp 后实际 width×height 超限抛 CropTooLargeError。

    在 read_region 之前调用（width/height 来自 osr.dimensions 元数据，
    读取元数据不解码像素）。
    """
    limit = CROP_MAX_PIXELS if max_pixels is None else int(max_pixels)
    w, h = int(width), int(height)
    if w <= 0 or h <= 0:
        raise ValueError("crop 尺寸需为正")
    if w * h > limit:
        raise CropTooLargeError(
            "裁剪区域 %d×%d=%d 像素超过单请求上限 %d，请缩小 size 后重试"
            % (w, h, w * h, limit))


class SlidingPixelWindow:
    """per-subject 60s 滑动窗口像素预算（进程内，线程安全）。

    与 app.py 插件通道的 _SlidingPixelWindow 同算法独立实现（crop_guard
    不 import app.py，避免主站/分享端循环依赖）；admit() 计入并返回
    (True, 0)，超限返回 (False, retry_after)。
    """

    def __init__(self, budget_per_min, window_sec=60):
        self._budget = int(budget_per_min)
        self._window = int(window_sec)
        self._lock = threading.Lock()
        self._buckets = {}  # subject -> deque[(ts, pixels)]

    def _evict(self, dq, now):
        cutoff = now - self._window
        while dq and dq[0][0] <= cutoff:
            dq.popleft()

    def admit(self, subject, pixels, now=None):
        now = time.time() if now is None else now
        with self._lock:
            dq = self._buckets.setdefault(subject, deque())
            self._evict(dq, now)
            total = sum(p for _, p in dq)
            if total + pixels <= self._budget:
                dq.append((now, pixels))
                return True, 0
            return False, self._retry_after(dq, pixels, now)

    def _retry_after(self, dq, pixels, now):
        if pixels > self._budget:
            return self._window
        need = (sum(p for _, p in dq) + pixels) - self._budget
        freed = 0
        for ts, p in dq:
            freed += p
            if freed >= need:
                return max(1, int(math.ceil((ts + self._window) - now)))
        return self._window


class _ConcurrencyGate:
    """进程级非阻塞信号量闸。"""

    def __init__(self, n):
        self._sem = threading.BoundedSemaphore(int(n))

    def acquire(self):
        return self._sem.acquire(blocking=False)

    def release(self):
        try:
            self._sem.release()
        except ValueError:  # pragma: no cover - 防御性：多余 release
            pass


# 进程级单例（主站与分享端各进程一份；combined_app 形态下同进程共享）。
# 测试用 monkeypatch 替换为小预算实例（对齐插件闸的测试形态）。
_PIXEL_WINDOW = SlidingPixelWindow(CROP_PIXEL_BUDGET_PER_MIN)
_CONCURRENCY_GATE = _ConcurrencyGate(CROP_MAX_CONCURRENT)


def admit_pixels(subject, pixels, window=None):
    """每分钟像素预算判定。返回 (allowed, retry_after_seconds)。"""
    win = _PIXEL_WINDOW if window is None else window
    return win.admit(subject, int(pixels))


def acquire_slot(gate=None):
    """取并发槽；返回句柄（True）或 None（忙）。"""
    g = _CONCURRENCY_GATE if gate is None else gate
    return True if g.acquire() else None


def release_slot(slot, gate=None):
    """归还并发槽（slot 为 acquire_slot 的返回值）。"""
    g = _CONCURRENCY_GATE if gate is None else gate
    if slot is not None:
        g.release()
