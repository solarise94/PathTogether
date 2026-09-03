# -*- coding: utf-8 -*-
"""P0-A §3.5 crop 像素闸测试（docs/open-registration-security-remediation §6.2）。

覆盖：
  - 主站 /api/slide/<name>/crop：超 CROP_MAX_PIXELS 在 read_region 前拒绝
    （413 crop_too_large，解码零调用）；clamp 后恰等于上限 → 放行；
  - 分享端 /s/<token>/api/slide/<name>/crop：同样在 read_region 前拒绝；
  - 每分钟像素预算（主站按 user、分享端按 token）超限 → 429 + Retry-After；
  - 并发闸占满 → 429 crop_busy；
  - 单元：check_pixel_limit 边界（== 上限放行，+1 拒绝）。

主站与分享端共用 crop_guard 同一实现——本测试同时压两端，防漂移。
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import crop_guard  # noqa: E402
import share_store  # noqa: E402
import share_server as share_srv  # noqa: E402
import app as app_mod  # noqa: E402
from _pt_helpers import install_json_login_limits, isolate_app # noqa: E402


DEFAULT_MAX_PIXELS = 4096 ** 2


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每用例：独立存储 + 三道闸复位为全新实例 + 上限钉默认值。"""
    _, up_dir = isolate_app(monkeypatch, tmp_path, UPLOAD_DIR,
                            login_limits=True)
    monkeypatch.setattr(crop_guard, "CROP_MAX_PIXELS", DEFAULT_MAX_PIXELS)
    monkeypatch.setattr(crop_guard, "_PIXEL_WINDOW",
                        crop_guard.SlidingPixelWindow(
                            crop_guard.CROP_PIXEL_BUDGET_PER_MIN))
    monkeypatch.setattr(crop_guard, "_CONCURRENCY_GATE",
                        crop_guard._ConcurrencyGate(
                            crop_guard.CROP_MAX_CONCURRENT))
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    yield


# --------------------------------------------------------------------------- #
# 辅助：slide 读路径 mock（dimensions 元数据可读，read_region 可计数）
# --------------------------------------------------------------------------- #
_FAKE_ENTRY = {"pool": None, "sem": None}


@contextmanager
def _borrow_pair_ctx(pair):
    yield pair


def _fake_osr(dimensions=(100000, 100000)):
    osr = mock.Mock(dimensions=dimensions)
    region = mock.Mock()
    region.convert.return_value = region
    osr.read_region.return_value = region
    return osr


def _mock_slide_read(mod, osr):
    pair = {"osr": osr}
    return (mock.patch.object(mod, "_get_slide", return_value=_FAKE_ENTRY),
            mock.patch.object(mod.slide_cache, "borrow_pair",
                              side_effect=lambda _e: _borrow_pair_ctx(pair)))


def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return app_mod.app.test_client()


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _touch(name="demo.svs"):
    p = Path(UPLOAD_DIR) / name
    p.write_bytes(b"svs-stub")
    return name


def _share_token(slide):
    share = share_store.create_share([slide], 1)
    return share["token"]


# =========================================================================== #
# 1. 单请求像素硬闸（read_region 前拒绝）
# =========================================================================== #
def test_main_crop_over_limit_413_before_read_region():
    """size=5000（clamp 后 5000²=25M > 4096²）→ 413，read_region 零调用。"""
    slide = _touch()
    osr = _fake_osr()
    m1, m2 = _mock_slide_read(app_mod, osr)
    with m1, m2:
        r = _client().get("/api/slide/%s/crop?x=0&y=0&size=5000" % slide)
    assert r.status_code == 413
    body = r.get_json()
    assert body["code"] == "crop_too_large"
    assert body["max_pixels"] == DEFAULT_MAX_PIXELS
    # 任何解码前拒绝：borrow_pair 只读 dimensions 元数据，read_region 零调用
    assert osr.read_region.call_count == 0


def test_main_crop_at_limit_passes():
    """clamp 后恰为 4096²（== 上限，非 >）→ 放行到 read_region。"""
    slide = _touch()
    osr = _fake_osr(dimensions=(4096, 4096))
    m1, m2 = _mock_slide_read(app_mod, osr)
    with m1, m2:
        r = _client().get("/api/slide/%s/crop?x=0&y=0&size=4096" % slide)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert osr.read_region.call_count == 1


def test_share_crop_over_limit_413_before_read_region():
    slide = _touch()
    token = _share_token(slide)
    osr = _fake_osr()
    m1, m2 = _mock_slide_read(share_srv, osr)
    with m1, m2:
        r = _share_client().get(
            "/s/%s/api/slide/%s/crop?x=0&y=0&size=40000" % (token, slide))
    assert r.status_code == 413
    assert r.get_json()["code"] == "crop_too_large"
    assert osr.read_region.call_count == 0


def test_share_crop_small_passes():
    slide = _touch()
    token = _share_token(slide)
    osr = _fake_osr()
    m1, m2 = _mock_slide_read(share_srv, osr)
    with m1, m2:
        r = _share_client().get(
            "/s/%s/api/slide/%s/crop?x=0&y=0&size=100" % (token, slide))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert osr.read_region.call_count == 1


def test_check_pixel_limit_boundaries():
    crop_guard.check_pixel_limit(4096, 4096)  # == 上限放行
    with pytest.raises(crop_guard.CropTooLargeError):
        crop_guard.check_pixel_limit(4096, 4097)
    with pytest.raises(crop_guard.CropTooLargeError):
        crop_guard.check_pixel_limit(1, DEFAULT_MAX_PIXELS + 1)


# =========================================================================== #
# 2. 每分钟像素预算（主站按 user / 分享端按 token）
# =========================================================================== #
def test_main_crop_budget_429(monkeypatch):
    """预算压小：单请求像素在硬闸之下但超预算 → 429 + Retry-After。"""
    slide = _touch()
    monkeypatch.setattr(crop_guard, "_PIXEL_WINDOW",
                        crop_guard.SlidingPixelWindow(1_000_000))  # 1M/分钟
    m1, m2 = _mock_slide_read(app_mod, _fake_osr())
    with m1, m2:
        r = _client().get("/api/slide/%s/crop?x=0&y=0&size=2000" % slide)
        # 2000²=4M > 1M 预算 → 429（仍在 4096² 硬闸内）
        assert r.status_code == 429
        assert r.get_json()["code"] == "crop_rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
        # 预算拒绝发生在解码前，且被拒请求不扣减预算
        r2 = _client().get("/api/slide/%s/crop?x=0&y=0&size=500" % slide)
        assert r2.status_code == 200, r2.get_data(as_text=True)  # 0.25M ≤ 1M


def test_share_crop_budget_per_token(monkeypatch):
    """预算按 token 计：同 token 第二次超预算 429，另一 token 不受影响。"""
    slide = _touch()
    token_a = _share_token(slide)
    token_b = _share_token(slide)
    monkeypatch.setattr(crop_guard, "_PIXEL_WINDOW",
                        crop_guard.SlidingPixelWindow(1_200_000))
    m1, m2 = _mock_slide_read(share_srv, _fake_osr())
    c = _share_client()
    with m1, m2:
        r1 = c.get("/s/%s/api/slide/%s/crop?x=0&y=0&size=1000" % (token_a, slide))
        assert r1.status_code == 200  # 1M ≤ 1.2M
        r2 = c.get("/s/%s/api/slide/%s/crop?x=0&y=0&size=500" % (token_a, slide))
        assert r2.status_code == 429  # 1M + 0.25M > 1.2M
        rb = c.get("/s/%s/api/slide/%s/crop?x=0&y=0&size=500" % (token_b, slide))
        assert rb.status_code == 200  # 另一 token 独立预算


# =========================================================================== #
# 3. 并发闸
# =========================================================================== #
def test_crop_busy_429_when_slots_exhausted(monkeypatch):
    """并发闸压到 1 并手工占满 → 429 crop_busy；释放后恢复。"""
    slide = _touch()
    monkeypatch.setattr(crop_guard, "_CONCURRENCY_GATE",
                        crop_guard._ConcurrencyGate(1))
    slot = crop_guard.acquire_slot()
    assert slot is not None
    m1, m2 = _mock_slide_read(app_mod, _fake_osr())
    with m1, m2:
        r = _client().get("/api/slide/%s/crop?x=0&y=0&size=100" % slide)
    assert r.status_code == 429
    assert r.get_json()["code"] == "crop_busy"
    crop_guard.release_slot(slot)
    with m1, m2:
        r2 = _client().get("/api/slide/%s/crop?x=0&y=0&size=100" % slide)
    assert r2.status_code == 200


def test_concurrency_slot_released_after_decode():
    """read_region 后槽归还：同 subject 连续请求不因槽泄漏 429。"""
    slide = _touch()
    m1, m2 = _mock_slide_read(app_mod, _fake_osr())
    with m1, m2:
        for _ in range(5):
            r = _client().get("/api/slide/%s/crop?x=0&y=0&size=100" % slide)
            assert r.status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
