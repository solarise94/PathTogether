# -*- coding: utf-8 -*-
"""COS fast 纯流式转发下载适配器测试（contract: gokapi-relay
docs/slide-relay-contract.md v1；设计 docs/cos-fast-goapi-relay-design-20261013.md）。

在 127.0.0.1 起 in-process ThreadingHTTPServer 模拟 fast relay（含故障模式）：
- transport=direct 不变：SDK 路径被调用、fast server 零连接；
- fast happy path：中间块/尾块经**真实 worker `_fetch_range`** 采纳，请求头逐项
  核对（key 百分号编码、版本、大小、单一 Range、Bearer、no gzip）；
- 非 206（200 整对象 / 4xx / 5xx）→ CosAdapterError 脱敏（无 token、无完整 key）；
- 206 头后早 EOF / stall → `_fetch_range` `(None, wire)`，wire=实收字节，不抛；
- 302 不被跟随；配置错误/未知 transport/明文非回环 → CosConfigMissing；
- 复用 worker 测试夹具跑端到端 process_downloading（PG 由 conftest 内嵌起）。
"""

import http.server
import json
import logging
import socket
import threading
import time
import urllib.parse

import pytest

import cos_client
import cos_config
import cos_fast_relay  # noqa: F401  # transport 分发目标，导入即覆盖
import cos_ingest_worker as ciw
import ingestion_store as ist

import test_cos_ingest_worker as wh  # noqa: E402  # FakeCos/_staged 复用
from test_cos_ingest_worker import _env, _mkjob, _admit, _payload  # noqa: E402,F401

_log = logging.getLogger("svs.cos_ingest")

_TOKEN = "unit-test-token-0123456789abcdef"
_KEY = "incoming/own/JOB1/deadbeefcafebabe"
_VERSION = "ver-1"


# --------------------------------------------------------------------------- #
# 模拟 fast relay（ThreadingHTTPServer + 故障模式）
# --------------------------------------------------------------------------- #
class _RelayHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.requests.append({
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items()},
        })
        mode = srv.mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://evil.example/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode and mode.startswith("status:"):
            _tag, _code, err = mode.split(":", 2) if mode.count(":") >= 2 \
                else (mode.split(":")[0], mode.split(":")[1], "")
            code = int(_code)
            body = json.dumps({"error": err}).encode()
            self.send_response(code)
            if code == 503 and err == "busy":
                self.send_header("Retry-After", "5")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mode == "full200":
            body = srv.object_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 正常 206：解析 Range
        start, end = _parse_range(self.headers.get("Range"))
        size = len(srv.object_bytes)
        chunk = srv.object_bytes[start:end + 1]
        if mode == "early_eof":
            # 声明完整长度但只发一部分就断流
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            half = chunk[: max(1, len(chunk) // 2)]
            self.wfile.write(half)
            self.wfile.flush()
            # 直接关闭连接（Content-Length 未满足 → 客户端 IncompleteRead）
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        if mode == "stall":
            self.send_response(206)
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.flush()
            time.sleep(srv.stall_seconds)
            return
        self.send_response(206)
        self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("X-HP-Version-Id", self.headers.get(
            "X-HP-Version-Id", ""))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(chunk)


def _parse_range(value):
    start_s, end_s = value[len("bytes="):].split("-")
    return int(start_s), int(end_s)


class RelayServer:
    def __init__(self, object_bytes=b"", mode=None, stall_seconds=30):
        self.requests = []
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _RelayHandler)
        # handler 通过 self.server.* 读取实例态
        self.httpd.object_bytes = object_bytes
        self.httpd.mode = mode
        self.httpd.stall_seconds = stall_seconds
        self.httpd.requests = self.requests
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        host, port = self.httpd.server_address[:2]
        return "http://%s:%d" % (host, port)

    def close(self):
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        self.thread.join(timeout=5)


@pytest.fixture
def relay_factory():
    servers = []

    def make(object_bytes=b"", mode=None, stall_seconds=30):
        srv = RelayServer(object_bytes, mode, stall_seconds)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.close()


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "fast_relay.token"
    p.write_text(_TOKEN + "\n", encoding="utf-8")
    p.chmod(0o600)
    return str(p)


def _enable_fast(monkeypatch, base_url, token_path, *, transport="fast"):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", transport)
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", base_url)
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE", token_path)
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_CONNECT_TIMEOUT", 5)
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_READ_TIMEOUT", 5)


# --------------------------------------------------------------------------- #
# 1) transport=direct 不变
# --------------------------------------------------------------------------- #
def test_direct_transport_uses_sdk_and_never_contacts_relay(
        monkeypatch, relay_factory, token_file):
    relay = relay_factory(mode="full200")
    # 即便配置齐备，direct（默认）也绝不碰 fast server
    _enable_fast(monkeypatch, relay.base_url, token_file, transport="direct")
    calls = []

    class _SdkClient:
        def get_object(self, **kw):
            calls.append(kw)
            return {"StatusCode": 206, "Content-Range": "bytes 0-3/10",
                    "Body": _Body(b"ABCD")}

    monkeypatch.setattr(cos_client, "_client", lambda: _SdkClient())
    monkeypatch.setattr(cos_client, "_bucket", lambda: "b-123")
    resp = cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-3",
                                 timeout=30, object_size=10)
    assert resp.read(4) == b"ABCD"
    assert calls and calls[0]["Range"] == "bytes=0-3"
    assert relay.requests == []  # fast server 零连接


class _Body:
    def __init__(self, data):
        self._data = data

    def get_stream(self):
        yield self._data


# --------------------------------------------------------------------------- #
# 2) fast happy path（经真实 worker _fetch_range）
# --------------------------------------------------------------------------- #
def test_fast_middle_and_tail_chunk_via_fetch_range(
        monkeypatch, relay_factory, token_file):
    payload = _payload(250)
    relay = relay_factory(object_bytes=payload)
    _enable_fast(monkeypatch, relay.base_url, token_file)

    # 中间块
    data, wire = ciw._fetch_range(
        cos_client, _KEY, _VERSION, 0, 99, 250)
    assert data == payload[:100]
    assert wire == 100
    # 尾块
    data2, wire2 = ciw._fetch_range(
        cos_client, _KEY, _VERSION, 200, 249, 250)
    assert data2 == payload[200:250]
    assert wire2 == 50

    # 请求头逐项
    hdrs = relay.requests[0]["headers"]
    assert hdrs["Authorization"] == "Bearer " + _TOKEN
    assert hdrs["X-HP-Object-Key"] == urllib.parse.quote(_KEY, safe="/")
    # key 的 "/" 未被编码
    assert "%2F" not in hdrs["X-HP-Object-Key"]
    assert hdrs["X-HP-Version-Id"] == _VERSION
    assert hdrs["X-HP-Object-Size"] == "250"
    assert hdrs["Range"] == "bytes=0-99"
    assert hdrs["Accept-Encoding"].lower() == "identity"


def test_fast_accept_encoding_never_gzip(monkeypatch, relay_factory, token_file):
    relay = relay_factory(object_bytes=_payload(20))
    _enable_fast(monkeypatch, relay.base_url, token_file)
    ciw._fetch_range(cos_client, _KEY, _VERSION, 0, 9, 20)
    raw = relay.requests[0]["headers"]
    assert "gzip" not in (raw.get("Accept-Encoding") or "").lower()


# --------------------------------------------------------------------------- #
# 3) 200 整对象 → CosAdapterError，数据不被采纳
# --------------------------------------------------------------------------- #
def test_fast_200_full_body_rejected(monkeypatch, relay_factory, token_file):
    relay = relay_factory(object_bytes=_payload(40), mode="full200")
    _enable_fast(monkeypatch, relay.base_url, token_file)
    with pytest.raises(cos_client.CosAdapterError) as ei:
        ciw._fetch_range(cos_client, _KEY, _VERSION, 0, 9, 40)
    assert "HTTP 200" in str(ei.value)


# --------------------------------------------------------------------------- #
# 4) 错误状态族 → CosAdapterError 脱敏
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,code", [
    (404, "source_missing"), (401, "unauthorized"), (403, "forbidden"),
    (416, "range_not_satisfiable"), (502, "upstream_error"),
    (503, "busy"), (504, "upstream_timeout"),
])
def test_fast_error_statuses_sanitized(
        monkeypatch, relay_factory, token_file, caplog, status, code):
    relay = relay_factory(mode="status:%d:%s" % (status, code))
    _enable_fast(monkeypatch, relay.base_url, token_file)
    with caplog.at_level(logging.DEBUG):
        _log.setLevel(logging.DEBUG)
        with pytest.raises(cos_client.CosAdapterError) as ei:
            cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                                  timeout=30, object_size=40)
    msg = str(ei.value)
    assert "HTTP %d" % status in msg
    assert code in msg
    # 脱敏：token 与完整 key 绝不出现在异常消息或日志
    for blob in (msg, caplog.text):
        assert _TOKEN not in blob
        assert _KEY not in blob
    _log.setLevel(logging.NOTSET)


# --------------------------------------------------------------------------- #
# 5) 206 后早 EOF → (None, wire)，不抛
# --------------------------------------------------------------------------- #
def test_fast_early_eof_returns_none_wire(monkeypatch, relay_factory, token_file):
    payload = _payload(100)
    relay = relay_factory(object_bytes=payload, mode="early_eof")
    _enable_fast(monkeypatch, relay.base_url, token_file)
    data, wire = ciw._fetch_range(cos_client, _KEY, _VERSION, 0, 49, 100)
    assert data is None
    assert 0 < wire < 50  # 实收字节计入 wire（不足声明长度）
    # wire 精确等于服务端实际写出的字节数
    assert wire == 25


# --------------------------------------------------------------------------- #
# 6) stall 超读超时 → (None, wire) 有界
# --------------------------------------------------------------------------- #
def test_fast_stalled_body_bounded(monkeypatch, relay_factory, token_file):
    payload = _payload(100)
    relay = relay_factory(object_bytes=payload, mode="stall",
                          stall_seconds=30)
    _enable_fast(monkeypatch, relay.base_url, token_file)
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_READ_TIMEOUT", 1)
    t0 = time.monotonic()
    data, wire = ciw._fetch_range(cos_client, _KEY, _VERSION, 0, 49, 100)
    elapsed = time.monotonic() - t0
    assert data is None
    assert wire == 0
    assert elapsed < 15  # 远小于服务端 30s stall，说明超时生效


# --------------------------------------------------------------------------- #
# 7) 302 不被跟随
# --------------------------------------------------------------------------- #
def test_fast_redirect_not_followed(monkeypatch, relay_factory, token_file):
    relay = relay_factory(mode="redirect")
    _enable_fast(monkeypatch, relay.base_url, token_file)
    with pytest.raises(cos_client.CosAdapterError) as ei:
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              timeout=30, object_size=40)
    assert "HTTP 302" in str(ei.value)


# --------------------------------------------------------------------------- #
# 8) 配置错误族 → CosConfigMissing
# --------------------------------------------------------------------------- #
def test_config_missing_url(monkeypatch, token_file):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "fast")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", "")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE", token_file)
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_config_missing_token_file(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "fast")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE", "")
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_config_empty_token_file(monkeypatch, tmp_path):
    p = tmp_path / "empty.token"
    p.write_text("\n\n", encoding="utf-8")
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "fast")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE", str(p))
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_config_unreadable_token_file(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "fast")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", "https://relay.test")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE",
                        "/nonexistent/dir/token")
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_unknown_transport_fail_closed(monkeypatch):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "turbo")
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_plain_http_non_loopback_rejected(monkeypatch, token_file):
    monkeypatch.setattr(cos_config, "COS_DOWNLOAD_TRANSPORT", "fast")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_URL", "http://relay.test")
    monkeypatch.setattr(cos_config, "COS_FAST_RELAY_TOKEN_FILE", token_file)
    with pytest.raises(cos_client.CosConfigMissing):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9",
                              object_size=40)


def test_fast_requires_object_size(monkeypatch, relay_factory, token_file):
    relay = relay_factory(object_bytes=_payload(20))
    _enable_fast(monkeypatch, relay.base_url, token_file)
    with pytest.raises(cos_client.CosAdapterError):
        cos_client.get_object(_KEY, _VERSION, range_header="bytes=0-9")
    assert relay.requests == []  # 参数缺失时不发请求


def test_fast_requires_range_header(monkeypatch, relay_factory, token_file):
    relay = relay_factory(object_bytes=_payload(20))
    _enable_fast(monkeypatch, relay.base_url, token_file)
    with pytest.raises(cos_client.CosAdapterError):
        cos_client.get_object(_KEY, _VERSION, object_size=20)
    assert relay.requests == []


# --------------------------------------------------------------------------- #
# 9) 多块循环（模拟 worker 下载循环的 Range 序列）
# --------------------------------------------------------------------------- #
def test_fast_multi_chunk_range_loop(monkeypatch, relay_factory, token_file):
    payload = _payload(250)
    relay = relay_factory(object_bytes=payload)
    _enable_fast(monkeypatch, relay.base_url, token_file)
    chunk = 100
    got = bytearray()
    offset = 0
    while offset < len(payload):
        end = min(offset + chunk, len(payload)) - 1
        data, wire = ciw._fetch_range(
            cos_client, _KEY, _VERSION, offset, end, len(payload))
        assert data is not None
        got += data
        offset += len(data)
    assert bytes(got) == payload
    assert [r["headers"]["Range"] for r in relay.requests] == [
        "bytes=0-99", "bytes=100-199", "bytes=200-249"]


# --------------------------------------------------------------------------- #
# 10) 端到端 process_downloading（transport=fast，复用 worker 夹具 + 内嵌 PG）
# --------------------------------------------------------------------------- #
def test_end_to_end_process_downloading_fast(
        monkeypatch, relay_factory, token_file):
    payload = _payload(150)
    relay = relay_factory(object_bytes=payload)
    _enable_fast(monkeypatch, relay.base_url, token_file)
    # worker 下载循环按 DOWNLOAD_CHUNK_BYTES 分块；夹具已缩到 30 字节
    monkeypatch.setattr(ciw, "DOWNLOAD_CHUNK_BYTES", 30)

    fake, st = wh.FakeCos(), {}
    job = _mkjob(size=150)
    assert _admit(job["job_id"])["outcome"] == "admitted"
    assert ciw.process_preparing(cos=fake, state=st) == job["job_id"]
    job = wh.ist.get_job(job["job_id"])
    for spec in job["part_plan_json"]:
        fake.put_part(job["object_key"], job["upload_id"],
                      spec["part_number"],
                      payload[spec["offset"]:spec["offset"] + spec["length"]])
    wh.ist.request_upload_complete(job["job_id"])
    assert ciw.process_completing(cos=fake, state=st) == job["job_id"]
    assert ciw.process_queued(cos=fake, state=st) == job["job_id"]

    # process_downloading 走**真实 cos_client.get_object** → fast 适配器 → 本地 server
    real_get = cos_client.get_object
    for _ in range(50):
        if wh.ist.get_job(job["job_id"])["state"] == wh.ist.VALIDATING:
            break
        assert ciw.process_downloading(cos=cos_client, state=st) == job["job_id"]
    out = wh.ist.get_job(job["job_id"])
    assert out["state"] == wh.ist.VALIDATING
    assert out["downloaded_bytes"] == 150
    staged = wh._staged(job["job_id"])
    with open(staged, "rb") as fh:
        assert fh.read() == payload
    assert len(relay.requests) >= 5  # 150/30 → 5 块
    assert real_get is cos_client.get_object  # 未被替换
