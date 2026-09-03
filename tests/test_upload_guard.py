# -*- coding: utf-8 -*-
"""P0-A §3.3 上传防护测试（docs/open-registration-security-remediation §6.2）。

覆盖：
  - 计数流（save_limited）：不信任声明长度，流式超限在上限处停止、无残留；
  - Werkzeug MAX_CONTENT_LENGTH 第一层拦截（稳定 413 JSON 信封）；
  - 端点：流式超限 413 无残留、成功走 .uploading-* 临时文件原子提升、
    目标冲突统一「名称不可用」（不回显真实文件名）、无效切片清理、
    磁盘保留水位 507；
  - PG 权威配额（RUN_PG_TESTS=1）：并发预占不越过 quota、失败释放
    reservation、成功转实占、在途/每小时限流、端点级配额与释放；
  - 后端差异：json 后端 role=user 上传 fail-closed 503；owner / 本地免登录
    不走配额（本地开发语义不变，两种后端一致）。

运行：cd 项目根 && python3 -m pytest tests/test_upload_guard.py -q
（PG 双跑：RUN_PG_TESTS=1 python3 -m pytest tests/test_upload_guard.py -q）
"""
import io
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import slide_io  # noqa: E402
import user_store  # noqa: E402
import upload_guard  # noqa: E402
import app as app_mod  # noqa: E402
from pg_compat import BACKEND  # noqa: E402
from _pt_helpers import csrf_client, install_json_login_limits, isolate_app, clear_upload_dir # noqa: E402
from _tiff_fixtures import make_ome_tiff_bytes, make_tiff_bytes  # noqa: E402


pg_only = pytest.mark.skipif(
    BACKEND != "postgres", reason="上传配额需 PG（RUN_PG_TESTS=1）")
json_only = pytest.mark.skipif(
    BACKEND != "json", reason="json 后端 fail-closed 行为仅在 json 模式断言")


# A0 异常契约后的验证 stub：成功返回 None / 失败抛 SlideValidationError，
# 签名兼容 format_hint 关键字（替代旧 lambda p: True/False 布尔契约）
def _validate_ok(path, **_):
    return None


def _validate_bad(path, **_):
    raise slide_io.SlideValidationError("invalid_slide", "无效的切片文件")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 无登录限制 mock + 防护参数复位 + 清空 uploads。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    # 防护参数复位（防环境变量/其它用例污染；水印置 0 使本机磁盘不干扰；
    # Werkzeug 层上限置 None = 放开，计数层单独收紧的用例自行 monkeypatch）
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 10 * 1024 ** 3)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", None)
    clear_upload_dir(up_dir)
    yield


def _client(auth=False):
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = auth
    return csrf_client(app_mod.app.test_client())


def _user_session(client, role="user", user_id="usr_test"):
    """直接注入登录 session（_require_auth 认 auth_user + 回查用户）。"""
    if user_id == "usr_test":
        u = user_store.create_user("quota@x.com", "pass1234pass1234", role=role)
        user_id = u["user_id"]
    with client.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = user_id
        sess["role"] = role
        # 批次 A：手工 session 需携带凭据版本（docs §6.2；新建用户=1）
        sess["auth_version"] = 1
        # 批次 A：手工 session 需携带与库内一致的凭据版本（docs §6.2）
        row = user_store.get_user(user_id)
        sess["auth_version"] = (row or {}).get("auth_version", 1)
    return user_id


def _upload(client, name="a.svs", size=100, content=None):
    data = content if content is not None else os.urandom(size)
    return client.post("/api/upload",
                       data={"file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data")


def _residue():
    """UPLOAD_DIR 中的临时残留（.uploading-* / .extracting-*）。"""
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-") or p.name.startswith(".extracting-")]


# =========================================================================== #
# 1. 计数流（不信任声明长度；无/伪造 Content-Length 都在上限处停止）
# =========================================================================== #
class _NoLenStream:
    """无 seek/len 信息的裸读流（模拟 chunked body 的 file.stream）。"""

    def __init__(self, data, chunk=64):
        self._buf = io.BytesIO(data)
        self._chunk = chunk

    def read(self, n=-1):
        return self._buf.read(self._chunk if n and n < 0 else min(n, self._chunk) or self._chunk)


def test_save_limited_stops_at_limit_no_residue(tmp_path):
    dst = tmp_path / "out.bin"
    with pytest.raises(upload_guard.RequestTooLarge):
        upload_guard.save_limited(io.BytesIO(b"x" * 5000), dst, limit=1000)
    assert not dst.exists()  # 半截文件已删


def test_save_limited_without_declared_length_counts_actual(tmp_path):
    dst = tmp_path / "out.bin"
    total = upload_guard.save_limited(_NoLenStream(b"y" * 3000), dst, limit=4000)
    assert total == 3000
    assert dst.stat().st_size == 3000


def test_save_limited_forged_small_limit_stream_longer(tmp_path):
    """伪造（偏小的）声明不放宽计数：实际读满上限即抛。"""
    dst = tmp_path / "out.bin"
    with pytest.raises(upload_guard.RequestTooLarge):
        upload_guard.save_limited(_NoLenStream(b"z" * 999999), dst, limit=200)
    assert not dst.exists()


def test_disk_watermark_check():
    with pytest.raises(upload_guard.DiskWatermarkExceeded):
        upload_guard.check_disk_watermark(
            Path(UPLOAD_DIR), need_bytes=0,
            reserved=10 ** 15)  # 不可能满足的水位
    # 水位 0 时应放行
    upload_guard.check_disk_watermark(Path(UPLOAD_DIR), need_bytes=0, reserved=0)


# =========================================================================== #
# 2. 端点（json/postgres 共同行为：单请求上限 + 原子提升 + 统一 409 + 水位）
# =========================================================================== #
def test_upload_success_uses_uploading_tmp_then_atomic(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    r = _upload(_client(), name="ok.svs", size=128)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["name"] == "ok.svs"
    assert (Path(UPLOAD_DIR) / "ok.svs").stat().st_size == 128
    assert _residue() == []


def test_upload_stream_over_limit_413_no_residue(monkeypatch):
    """计数层超限（Werkzeug 层已被 isolate 置 None 放开）：413、无残留。"""
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_REQUEST_BYTES", 1000)
    r = _upload(_client(), name="big.svs", size=5000)
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_too_large"
    assert not (Path(UPLOAD_DIR) / "big.svs").exists()
    assert _residue() == []


def test_upload_werkzeug_layer_413(monkeypatch):
    """第一层（MAX_CONTENT_LENGTH）：CL 超限在读体前/中即拒，稳定 JSON 信封。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    monkeypatch.setitem(app_mod.app.config, "MAX_CONTENT_LENGTH", 1000)
    r = _upload(_client(), name="big2.svs", size=5000)
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_too_large"
    assert not (Path(UPLOAD_DIR) / "big2.svs").exists()


def test_upload_conflict_unified_409_no_name_leak(monkeypatch):
    """目标已存在 → 统一「名称不可用」，不回显冲突文件名（docs §3.12）。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    (Path(UPLOAD_DIR) / "dup.svs").write_bytes(b"existing")
    r = _upload(_client(), name="dup.svs", size=10)
    assert r.status_code == 409
    assert "不可用" in r.get_json()["error"]
    assert "dup.svs" not in r.get_json()["error"]
    assert (Path(UPLOAD_DIR) / "dup.svs").read_bytes() == b"existing"  # 未被覆盖
    assert _residue() == []


def test_upload_invalid_slide_cleans_up(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_bad)
    r = _upload(_client(), name="bad.svs", size=32)
    assert r.status_code == 400
    assert r.get_json()["code"] == "invalid_slide"  # A0 稳定机器码
    assert not (Path(UPLOAD_DIR) / "bad.svs").exists()
    assert _residue() == []


# =========================================================================== #
# 2.5 真 TIFF 端到端（A0：V1 验证不 monkeypatch，真实字节走 .part+hint）
# =========================================================================== #
def test_upload_real_tiff_end_to_end_no_monkeypatch():
    """真 TIFF 经 V1（.part 临时名 + 净化名 hint）真验证后提升，中文空格名。"""
    tiff = make_tiff_bytes()
    name = "0702-L2-2 鼠奥球.tiff"
    r = _upload(_client(), name=name, content=tiff)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["name"] == name
    dest = Path(UPLOAD_DIR) / name
    assert dest.read_bytes() == tiff
    assert _residue() == []


def test_upload_real_garbage_tiff_rejected_with_stable_code():
    """垃圾字节伪装 .tif：真验证（无 monkeypatch）→ 400 invalid_slide，清理。"""
    r = _upload(_client(), name="junk.tif", content=b"\x00not-a-tiff" * 8)
    assert r.status_code == 400
    j = r.get_json()
    assert j["code"] == "invalid_slide"
    assert not (Path(UPLOAD_DIR) / "junk.tif").exists()
    assert _residue() == []


def test_upload_disk_watermark_507(monkeypatch):
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 10 ** 15)
    r = _upload(_client(), name="wm.svs", size=16)
    assert r.status_code == 507
    assert r.get_json()["code"] == "disk_watermark_exceeded"
    assert _residue() == []


def test_upload_owner_role_skips_quota(monkeypatch):
    """owner（有 user_id）不走配额——两种后端一致（本地开发语义不变）。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    _user_session(c, role="owner")
    r = _upload(c, name="own.svs", size=32)
    assert r.status_code == 200
    assert (Path(UPLOAD_DIR) / "own.svs").exists()


# =========================================================================== #
# 3. PG 权威配额 / reservation / 限流（RUN_PG_TESTS=1）
# =========================================================================== #
if BACKEND == "postgres":
    import psycopg

    def _set_quota(user_id, quota_bytes):
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                    "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                    "SET quota_bytes = EXCLUDED.quota_bytes",
                    (user_id, quota_bytes))
else:
    def _set_quota(user_id, quota_bytes):  # pragma: no cover
        raise RuntimeError("PG only")


@pg_only
def test_quota_row_created_with_env_default(monkeypatch):
    uid = user_store.create_user("q1@x.com", "pass1234pass1234", role="user")["user_id"]
    row = upload_guard.get_quota_row(uid)
    assert row["quota_bytes"] == upload_guard.UPLOAD_USER_QUOTA_BYTES
    assert row["used_bytes"] == 0 and row["reserved_bytes"] == 0


@pg_only
def test_concurrent_reservations_cannot_bypass_quota():
    """并发预占不越过 quota：Σ(reserved) ≤ quota，且无部分写。"""
    uid = user_store.create_user("q2@x.com", "pass1234pass1234", role="user")["user_id"]
    quota = 10000
    _set_quota(uid, quota)
    results = []
    lock = threading.Lock()

    def worker():
        try:
            r = upload_guard.reserve_upload(uid, 3000, inflight_limit=100,
                                            hourly_limit=100)
            ok = True
        except upload_guard.UploadGuardError:
            r, ok = None, False
        with lock:
            results.append((ok, r))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok_reserved = [r["reserved_bytes"] for ok, r in results if ok]
    # 10000 / 3000 → 至多 3 个成功；成功者之和 ≤ quota
    assert len(ok_reserved) <= 3
    assert sum(ok_reserved) <= quota
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] + row["reserved_bytes"] <= quota
    assert row["reserved_bytes"] == sum(ok_reserved)


@pg_only
def test_failure_releases_reservation():
    uid = user_store.create_user("q3@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10000)
    r = upload_guard.reserve_upload(uid, 6000, inflight_limit=10, hourly_limit=10)
    # 不释放时第二笔 6000 会因配额不足失败
    with pytest.raises(upload_guard.QuotaExceeded):
        upload_guard.reserve_upload(uid, 6000, inflight_limit=10, hourly_limit=10)
    upload_guard.release_reservation(r["reservation_id"])
    row = upload_guard.get_quota_row(uid)
    assert row["reserved_bytes"] == 0
    # 释放后同额度可再预占
    r2 = upload_guard.reserve_upload(uid, 6000, inflight_limit=10, hourly_limit=10)
    assert r2["reserved_bytes"] == 6000


@pg_only
def test_consume_converts_reserved_to_used():
    uid = user_store.create_user("q4@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10000)
    r = upload_guard.reserve_upload(uid, 5000, inflight_limit=10, hourly_limit=10)
    upload_guard.consume_reservation(r["reservation_id"], 3000)
    row = upload_guard.get_quota_row(uid)
    assert row["used_bytes"] == 3000
    assert row["reserved_bytes"] == 0
    # consumed 后不能再释放（防误退款语义与 ai_budget 一致）
    out = upload_guard.release_reservation(r["reservation_id"])
    assert out["state"] == "consumed"
    assert upload_guard.get_quota_row(uid)["used_bytes"] == 3000


@pg_only
def test_inflight_and_hourly_limits():
    uid = user_store.create_user("q5@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 9)
    # 在途：默认上限内成功，超限 429 语义异常
    for _ in range(upload_guard.UPLOAD_MAX_INFLIGHT):
        upload_guard.reserve_upload(uid, 100)
    with pytest.raises(upload_guard.InflightLimitExceeded):
        upload_guard.reserve_upload(uid, 100)
    # 每小时：显式小上限（独立用户），尝试次数计满即拒
    uid2 = user_store.create_user("q6@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid2, 10 ** 9)
    for _ in range(2):
        r = upload_guard.reserve_upload(uid2, 100, inflight_limit=10,
                                        hourly_limit=2)
        upload_guard.release_reservation(r["reservation_id"])
    with pytest.raises(upload_guard.RateLimitExceeded):
        upload_guard.reserve_upload(uid2, 100, inflight_limit=10, hourly_limit=2)


@pg_only
def test_expired_reservation_reclaimed_on_next_reserve(monkeypatch):
    uid = user_store.create_user("q7@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 5000)
    r = upload_guard.reserve_upload(uid, 4000, inflight_limit=10, hourly_limit=10)
    # 把该预占改为已过期
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE upload_reservations SET expires_at = now() - "
                        "interval '1 second' WHERE reservation_id = %s",
                        (r["reservation_id"],))
    # 过期量被惰性回收：新预占可用全额
    r2 = upload_guard.reserve_upload(uid, 5000, inflight_limit=10, hourly_limit=10)
    assert r2["reserved_bytes"] == 5000
    row = upload_guard.get_quota_row(uid)
    assert row["reserved_bytes"] == 5000


@pg_only
def test_endpoint_quota_denied_and_released(monkeypatch):
    uid = user_store.create_user("q8@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 1000)
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        # 批次 A：手工 session 需携带凭据版本（docs §6.2；新建用户=1）
        sess["auth_version"] = 1
    r = _upload(c, name="big3.svs", size=5000)
    assert r.status_code == 413
    assert r.get_json()["code"] == "upload_quota_exceeded"
    assert not (Path(UPLOAD_DIR) / "big3.svs").exists()
    assert _residue() == []
    # 预占已释放（无泄漏）
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 0


@pg_only
def test_endpoint_success_consumes_actual_bytes(monkeypatch):
    uid = user_store.create_user("q9@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 7)
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        # 批次 A：手工 session 需携带凭据版本（docs §6.2；新建用户=1）
        sess["auth_version"] = 1
    r = _upload(c, name="ok2.svs", size=777)
    assert r.status_code == 200, r.get_data(as_text=True)
    row = upload_guard.get_quota_row(uid)
    # 成功转实占：按实际落盘字节（非 CL / 预占提示值）
    assert row["used_bytes"] == 777
    assert row["reserved_bytes"] == 0


@pg_only
def test_endpoint_inflight_429(monkeypatch):
    uid = user_store.create_user("q10@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 9)
    # 在途上限压到 1，并预先占满名额
    monkeypatch.setattr(upload_guard, "UPLOAD_MAX_INFLIGHT", 1)
    upload_guard.reserve_upload(uid, 100)
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        # 批次 A：手工 session 需携带凭据版本（docs §6.2；新建用户=1）
        sess["auth_version"] = 1
    r = _upload(c, name="inf.svs", size=50)
    assert r.status_code == 429
    assert r.get_json()["code"] == "upload_inflight_limit"
    assert not (Path(UPLOAD_DIR) / "inf.svs").exists()
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 100  # 原预占未动


@pg_only
def test_endpoint_zip_failure_releases_reservation(monkeypatch):
    """非法 zip → 400，预占释放、无残留。"""
    import zipfile
    uid = user_store.create_user("q11@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 9)
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    with c.session_transaction() as sess:
        sess["auth_user"] = True
        sess["user_id"] = uid
        sess["role"] = "user"
        # 批次 A：手工 session 需携带凭据版本（docs §6.2；新建用户=1）
        sess["auth_version"] = 1
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", b"no slide here")
    r = c.post("/api/upload",
               data={"file": (io.BytesIO(buf.getvalue()), "bad.zip")},
               content_type="multipart/form-data")
    assert r.status_code == 400
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 0
    assert _residue() == []


# =========================================================================== #
# 4. 后端差异：json fail-closed（不退化进程内计数）
# =========================================================================== #
@json_only
def test_json_backend_user_upload_fail_closed(monkeypatch):
    """json 后端 + role=user：配额不可用 → 503（与 POST /login 同哲学）。"""
    monkeypatch.setattr(app_mod, "_validate_slide_file", _validate_ok)
    c = _client(auth=True)
    _user_session(c, role="user")
    r = _upload(c, name="j.svs", size=16)
    assert r.status_code == 503
    assert r.get_json()["code"] == "upload_guard_unavailable"
    assert not (Path(UPLOAD_DIR) / "j.svs").exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
