# -*- coding: utf-8 -*-
"""平台客户端 + 传输层测试（§1.1/§1.8/§1.2/§1.3 的客户端行为）。"""

import time

import pytest

from worker import errors
from worker.http_client import Transport

pytestmark = pytest.mark.c5b


# --------------------------------------------------------------------------- #
# §1.8 retryable 表
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code,expected", [
    ("import_not_found", False),
    ("import_state_invalid", False),
    ("idempotency_conflict", False),
    ("offset_conflict", False),
    ("checksum_mismatch", False),
    ("incomplete_write", False),
    ("commit_in_progress", False),
    ("size_exceeded", False),
    ("upload_quota_exceeded", False),
    ("disk_watermark_exceeded", True),
    ("format_unsupported", False),
    ("import_grant_invalid", False),
    ("cleanup_not_verified", False),
    ("rate_limited", True),
    ("token_expired", True),
    ("unauthorized", False),
    ("internal", True),
    ("unavailable", True),
])
def test_error_retryable_table(code, expected):
    assert errors.default_retryable(code) is expected


def test_unknown_code_not_retryable():
    assert errors.default_retryable("never_seen_code") is False


# --------------------------------------------------------------------------- #
# transport：退避 / Retry-After / 非 retryable 立即抛
# --------------------------------------------------------------------------- #

class _RawResp:
    def __init__(self, status, headers, body):
        import json as _json
        self.status_code = status
        self.headers = dict(headers)
        self.content = _json.dumps(body).encode("utf-8")


class _ScriptedSession:
    """替身 session：按脚本回放（RawResp / 异常），记录调用数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def request(self, method, url, **kw):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _RawResp(*item)

    def close(self):
        pass


def _transport(script, *, max_attempts=3, backoff_base=0.001,
               rate_limit_max_waits=8, sleeps=None):
    return Transport(
        "http://stub.invalid", max_attempts=max_attempts,
        backoff_base=backoff_base,
        rate_limit_max_waits=rate_limit_max_waits,
        sleep=(sleeps.append if sleeps is not None else None),
        session=_ScriptedSession(script))


def test_transport_retries_5xx_with_backoff():
    sleeps = []
    t = _transport([
        (500, {}, {"error": {"code": "internal", "message": "x",
                             "retryable": True}}),
        (200, {}, {"ok": 1}),
    ], max_attempts=3, backoff_base=0.01, sleeps=sleeps)
    resp = t.request("GET", "/x")
    assert resp.status == 200
    assert t._session.calls == 2
    assert sleeps == [0.01]


def test_transport_rate_limit_honours_retry_after():
    sleeps = []
    t = _transport([
        (429, {"Retry-After": "3"},
         {"error": {"code": "rate_limited", "message": "x",
                    "retryable": True, "details": {"retry_after": 3}}}),
        (200, {}, {"ok": 1}),
    ], rate_limit_max_waits=4, sleeps=sleeps)
    resp = t.request("GET", "/x")
    assert resp.status == 200
    assert sleeps == [3]  # 头优先，取整秒


def test_transport_non_retryable_raises_immediately():
    t = _transport([
        (409, {}, {"error": {"code": "offset_conflict", "message": "x",
                             "retryable": False,
                             "details": {"expected_offset": 5}}}),
    ], max_attempts=5)
    with pytest.raises(errors.ContractError) as ei:
        t.request("POST", "/x")
    assert ei.value.code == "offset_conflict"
    assert ei.value.details["expected_offset"] == 5
    assert t._session.calls == 1


def test_transport_exhaustion_raises_contract():
    t = _transport([
        (503, {}, {"error": {"code": "unavailable", "message": "x",
                             "retryable": True}})] * 3,
        max_attempts=3)
    with pytest.raises(errors.ContractError) as ei:
        t.request("GET", "/x")
    assert ei.value.code == "unavailable"
    assert t._session.calls == 3


def test_transport_connection_error_response_lost():
    import requests
    t = _transport([
        requests.exceptions.ConnectionError("boom"),
    ] * 3, max_attempts=3)
    with pytest.raises(errors.TransportError) as ei:
        t.request("POST", "/x", response_lost_capable=True)
    assert ei.value.response_lost is True


# --------------------------------------------------------------------------- #
# 真实 stub 平台上的客户端行为（§1.2/§1.3）
# --------------------------------------------------------------------------- #

def test_begin_idempotent_replay_no_second_write_token(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    ctx = sandbox.build_ctx()
    from worker.item_task import idempotency_key_for
    item = sandbox.state.items["itm_bch_c5b_0"]
    key = idempotency_key_for("inst_c5b", "bch_c5b", item["item_id"])
    r1 = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=10,
        scratch_bytes=0, idempotency_key=key)
    assert r1.get("write_token")
    # 同键同载荷重放：同 import_id、不重发 write_token（§1.2）
    r2 = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=10,
        scratch_bytes=0, idempotency_key=key)
    assert r2["import_id"] == r1["import_id"]
    assert "write_token" not in r2
    # 同键异载荷 → 409 idempotency_conflict（declared_size 漂移）
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_begin(
            grant_id="pig_c5b_main", project_id="prj_test",
            filename="x.tif", format_ext="tif", declared_size=99,
            scratch_bytes=0, idempotency_key=key)
    assert ei.value.code == "idempotency_conflict"
    assert sandbox.state.counters["begin"] == 3


def test_write_chunk_sha_mismatch_raw_http(sandbox):
    """块 sha256 篡改 → 409 checksum_mismatch（raw HTTP 直打 stub）。"""
    import hashlib

    import requests as rq
    ctx = sandbox.build_ctx()
    r = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=10,
        scratch_bytes=0, idempotency_key="k_sha")
    jwt = ctx.platform.ensure_token()
    resp = rq.post(
        "%s/api/plugin/v1/imports/%s/write" % (sandbox.base_url,
                                               r["import_id"]),
        data=b"0123456789",
        headers={"Authorization": "Bearer " + jwt,
                 "X-Import-Token": r["write_token"],
                 "X-Import-Offset": "0",
                 "X-Import-Chunk-Sha256": "0" * 64,
                 "Content-Type": "application/octet-stream"},
        timeout=10)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "checksum_mismatch"
    # 正确摘要可重发（块作废不推进 offset）
    resp2 = rq.post(
        "%s/api/plugin/v1/imports/%s/write" % (sandbox.base_url,
                                               r["import_id"]),
        data=b"0123456789",
        headers={"Authorization": "Bearer " + jwt,
                 "X-Import-Token": r["write_token"],
                 "X-Import-Offset": "0",
                 "X-Import-Chunk-Sha256":
                     hashlib.sha256(b"0123456789").hexdigest(),
                 "Content-Type": "application/octet-stream"},
        timeout=10)
    assert resp2.status_code == 200
    assert resp2.json()["confirmed_offset"] == 10


def test_write_token_mismatch_forbidden(sandbox):
    ctx = sandbox.build_ctx()
    key = "k_test_wt"
    r = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=10,
        scratch_bytes=0, idempotency_key=key)
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_write_chunk(r["import_id"], "wt_wrong",
                                        0, b"0123456789")
    assert ei.value.code == "forbidden"
    assert sandbox.state.counters["write_token_rejected"] == 1


def test_write_requires_exact_offset_and_topup(sandbox):
    ctx = sandbox.build_ctx()
    r = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=10,
        scratch_bytes=0, idempotency_key="k_off")
    iid, tok = r["import_id"], r["write_token"]
    # 错 offset → 409 offset_conflict + expected_offset（§1.3 第 3 步）
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_write_chunk(iid, tok, 4, b"0123456789")
    assert ei.value.code == "offset_conflict"
    assert ei.value.details["expected_offset"] == 0
    # 越界（写前容量闸）→ 413 size_exceeded（先 topup，§4.3）
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_write_chunk(iid, tok, 0, b"0" * 11)
    assert ei.value.code == "size_exceeded"
    # topup 后可写
    ctx.platform.import_topup(iid, tok, 10)
    out = ctx.platform.import_write_chunk(iid, tok, 0, b"0123456789")
    assert out["confirmed_offset"] == 10


def test_grant_reason_enriched_on_begin(sandbox):
    sandbox.state.revoke_grant("pig_c5b_main")
    ctx = sandbox.build_ctx()
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_begin(
            grant_id="pig_c5b_main", project_id="prj_test",
            filename="x.tif", format_ext="tif", declared_size=1,
            scratch_bytes=0, idempotency_key="k_revoked")
    assert ei.value.code == "import_grant_invalid"
    assert ei.value.reason == "grant_revoked"


def test_client_reauth_on_401_unauthorized_not_retried(sandbox):
    """installation 停用 → 401 unauthorized（非 token_expired）立即失败。"""
    ctx = sandbox.build_ctx()
    ctx.platform.ensure_token()
    sandbox.state.enabled = False
    with pytest.raises(errors.ContractError) as ei:
        ctx.platform.import_status("pim_whatever")
    assert ei.value.code == "unauthorized"


def test_429_real_http_honoured(sandbox):
    """真 HTTP 429 + Retry-After：客户端等待后重试成功（计时断言）。"""
    sandbox.state.rate_limits["/api/plugin/v1/imports/"] = {
        "n": 1, "window": 0.9, "retry_after": 1}
    ctx = sandbox.build_ctx()
    r = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=8,
        scratch_bytes=0, idempotency_key="k_rl_1")
    t0 = time.monotonic()
    out = ctx.platform.import_write_chunk(
        r["import_id"], r["write_token"], 0, b"01234567")
    elapsed = time.monotonic() - t0
    assert out["confirmed_offset"] == 8
    assert elapsed >= 0.9, "应等待 Retry-After 指示的时间"
    assert sandbox.state.counters.get("rate_limited", 0) >= 1
