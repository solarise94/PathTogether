# -*- coding: utf-8 -*-
"""slide_cache 文件换代（review 2026-08-29 G3 / CACHE-1）回归。

覆盖验收矩阵 G3：
- 同名 ``os.replace``、原地改写、**保持 mtime+size** 的同名替换均换
  generation（签名含 dev/ino/size/mtime_ns，不只比 mtime）；
- 旧代句柄 retire：池内空闲句柄换代时立即关；在途句柄可完成请求，归还即关；
- 并发借用期间换代不串代、不漏关；
- info（cached_read_metadata）与 tile（share_server 瓦片缓存）不跨代命中；
- 读取前后签名持续变化 → 丢弃重试一次后 SlideFileChanged（fail-closed）；
- 主站 evict 仅快速路径：不 evict 时借用照样换代（跨进程语义的本进程等价）。

切片文件用临时文件 + 假句柄工厂（monkeypatch slide_cache._make_pair）模拟，
不依赖真实 OpenSlide 文件；文件签名全部走真实 stat。
"""
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

import _bootstrap  # noqa: F401  # session 目录+openslide stub（conftest 先行）

sys.path.insert(0, str(Path(__file__).resolve().parent))

import share_store  # noqa: E402
import slide_cache  # noqa: E402
import share_server as share_srv  # noqa: E402


# --------------------------------------------------------------------------- #
# 假句柄工厂：pair 内容随文件当前 marker 变化；close 记录用于断言
# --------------------------------------------------------------------------- #
class FakeOsr:
    def __init__(self, marker, tracker):
        self.marker = marker
        self._tracker = tracker
        self.dimensions = (1000, 1000)
        self.properties = {}
        self.closed = False

    def close(self):
        self.closed = True
        self._tracker.append(self.marker)


class FakeDz:
    def __init__(self, marker):
        self.marker = marker
        self.level_dimensions = [(1000, 1000)]

    def get_tile(self, level, addr):
        # marker 第二字符（版本数字）决定颜色，跨代内容可区分
        img = Image.new("RGB", (8, 8), color=(ord(self.marker[1]) % 256, 0, 0))
        return img


@pytest.fixture
def fake_pairs(tmp_path, monkeypatch):
    """安装假 _make_pair：按文件当前内容建 (osr, dz)；返回 close 追踪器。"""
    closed = []

    def _make(path, generation):
        marker = Path(path).read_text(encoding="utf-8")
        return {"osr": FakeOsr(marker, closed), "dz": FakeDz(marker),
                "gen": generation}

    monkeypatch.setattr(slide_cache, "_make_pair", _make)
    return closed


def _slide_cache_reset():
    """清空模块级缓存（跨用例隔离；与现有测试清理 _info_cache 的做法一致）。"""
    with slide_cache._cache_lock:
        slide_cache._slide_cache.clear()
    with slide_cache._info_cache_lock:
        slide_cache._info_cache.clear()


@pytest.fixture(autouse=True)
def _clean_caches():
    _slide_cache_reset()
    yield
    _slide_cache_reset()


@pytest.fixture
def slide_file(tmp_path):
    p = tmp_path / "demo.svs"
    p.write_text("v1", encoding="utf-8")
    return p


def _replace_with(p: Path, marker: str):
    """同名替换（新 inode）：tmp 写 marker 后 os.replace。"""
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(marker, encoding="utf-8")
    os.replace(tmp, p)


def _inplace_rewrite(p: Path, marker: str):
    """原地改写（同 inode，size/mtime 变）。"""
    with open(p, "r+b") as f:
        f.write(marker.encode("utf-8"))
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def _replace_preserving_stat(p: Path, marker: str):
    """同名替换且把 size 与 mtime_ns 恢复成旧值：签名只剩 ino 可判别。"""
    st = os.stat(p)
    marker = marker.ljust(st.st_size, "#")  # size 保持一致
    tmp = p.with_name(p.name + ".new")
    tmp.write_text(marker, encoding="utf-8")
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, p)


REPLACERS = {
    "os.replace 换 inode": _replace_with,
    "原地改写（同 inode）": _inplace_rewrite,
    "保持 mtime+size 的替换": _replace_preserving_stat,
}


# --------------------------------------------------------------------------- #
# 1) 各类替换均换 generation，句柄换新
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("how", sorted(REPLACERS))
def test_every_replacement_switches_generation(slide_file, fake_pairs, how):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    with slide_cache.borrow_pair(entry) as p1:
        assert p1["osr"].marker == "v1"
        gen1 = p1["gen"]
    assert gen1 == 1  # 首次借用建立第 1 代

    REPLACERS[how](slide_file, "v2")
    with slide_cache.borrow_pair(entry) as p2:
        assert p2["osr"].marker == "v2", "%s 后必须读新内容" % how
        assert p2["gen"] > gen1, "%s 后必须换 generation" % how
        gen2 = p2["gen"]
    assert gen2 == gen1 + 1
    # 旧代空闲句柄已立即关闭
    assert "v1" in fake_pairs, "旧代空闲句柄应立即关闭"


def test_mtime_only_comparison_would_miss(slide_file, fake_pairs):
    """保持 mtime+size 的替换：旧实现（只比 mtime）会漏判，新签名必须命中。"""
    entry = slide_cache.get_slide("demo.svs", slide_file)
    with slide_cache.borrow_pair(entry) as p1:
        mtime = os.stat(slide_file).st_mtime_ns
        size = os.stat(slide_file).st_size
    _replace_preserving_stat(slide_file, "v9")
    assert os.stat(slide_file).st_mtime_ns == mtime  # mtime 未变
    assert os.stat(slide_file).st_size == size      # size 未变
    with slide_cache.borrow_pair(entry) as p2:
        assert p2["osr"].marker.startswith("v9"), "inode 变化必须触发换代"


# --------------------------------------------------------------------------- #
# 2) retire/return：在途旧代可完成，归还即关
# --------------------------------------------------------------------------- #
def test_inflight_old_pair_completes_then_closes(slide_file, fake_pairs):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    with slide_cache.borrow_pair(entry) as old_pair:
        old_gen = old_pair["gen"]
        old_marker = old_pair["osr"].marker
        # 借用期间文件被替换
        _replace_with(slide_file, "v2")
        # 在途请求可继续用旧句柄完成（一致性旧内容）
        assert old_pair["osr"].marker == old_marker
        assert not old_pair["osr"].closed

        # 新借用走新代
        with slide_cache.borrow_pair(entry) as new_pair:
            assert new_pair["gen"] == old_gen + 1
            assert new_pair["osr"].marker == "v2"
            assert not new_pair["osr"].closed
        # 旧代在途句柄归还即关
        assert not old_pair["osr"].closed, "归还发生在 with 退出时"
    assert old_pair["osr"].closed, "旧代在途句柄归还时必须关闭"

    # 池内只剩当前代句柄
    pooled = []
    while True:
        try:
            pooled.append(entry["pool"].get_nowait())
        except queue.Empty:
            break
    assert pooled and all(p["gen"] == old_gen + 1 for p in pooled)
    for p in pooled:
        entry["pool"].put(p)


def test_idle_old_pairs_closed_immediately_on_refresh(slide_file, fake_pairs):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    with slide_cache.borrow_pair(entry) as p:
        pass  # 归还池内（空闲）
    assert not p["osr"].closed
    _replace_with(slide_file, "v2")
    assert slide_cache.refresh_generation(entry) == 2
    assert p["osr"].closed, "换代时池内空闲旧代句柄必须立即关闭"
    assert entry["retired"] is True


# --------------------------------------------------------------------------- #
# 3) 并发借用 + 连续换代：不串代、无泄漏
# --------------------------------------------------------------------------- #
def test_concurrent_borrow_with_replacements(slide_file, fake_pairs):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    errors = []
    seen_gens = set()
    lock = threading.Lock()
    stop = threading.Event()

    def _borrower():
        try:
            while not stop.is_set():
                with slide_cache.borrow_pair(entry) as pair:
                    marker = pair["osr"].marker
                    # 句柄内容只能来自某一完整版本
                    assert marker[0] == "v" and marker[1:].rstrip("#").isdigit(), marker
                    with lock:
                        seen_gens.add(pair["gen"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_borrower) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        for i in range(2, 8):
            _replace_with(slide_file, "v%d" % i)
            # 真实替换（人工重传）有间隔：给在途借用机会收敛到当前代。
            # 连续微秒级替换风暴下借用按设计 fail-closed（SlideFileChanged），
            # 那是 read_stable 层「最多重试一次」语义，不属于本用例断言。
            time.sleep(0.02)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    assert errors == [], "并发借用不得报错：%s" % errors
    assert len(seen_gens) >= 3, "连续替换应产生多代，实际 %s" % sorted(seen_gens)
    # 收尾：全部存活句柄都是当前代
    with slide_cache.borrow_pair(entry) as final_pair:
        current_gen = final_pair["gen"]
    pooled = []
    while True:
        try:
            pooled.append(entry["pool"].get_nowait())
        except queue.Empty:
            break
    assert all(p["gen"] == current_gen for p in pooled)
    for p in pooled:
        entry["pool"].put(p)


# --------------------------------------------------------------------------- #
# 4) info：不跨代命中（签名键控 + 读后校验）
# --------------------------------------------------------------------------- #
def test_metadata_cache_no_cross_generation_hit(slide_file, fake_pairs):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    calls = []

    def _read_meta():
        with slide_cache.borrow_pair(entry) as pair:
            calls.append(pair["osr"].marker)
            return {"marker": pair["osr"].marker}

    m1 = slide_cache.cached_read_metadata("demo.svs", slide_file, _read_meta)
    assert m1["marker"] == "v1"
    # 未换文件：缓存命中（不再读）
    m1b = slide_cache.cached_read_metadata("demo.svs", slide_file, _read_meta)
    assert m1b["marker"] == "v1"
    assert calls == ["v1"]

    _replace_preserving_stat(slide_file, "v2")  # mtime 保持也要失效
    m2 = slide_cache.cached_read_metadata("demo.svs", slide_file, _read_meta)
    assert m2["marker"].startswith("v2"), "换文件后不得命中旧缓存"
    assert calls == ["v1", "v2"]


def test_metadata_retry_once_when_changed_during_read(slide_file, fake_pairs, monkeypatch):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    reads = []

    def _read_meta():
        with slide_cache.borrow_pair(entry) as pair:
            marker = pair["osr"].marker
            reads.append(marker)
            if len(reads) == 1:
                # 首次读取期间文件被替换（读后签名变化）
                _replace_with(slide_file, "v2")
            return {"marker": marker}

    meta = slide_cache.cached_read_metadata("demo.svs", slide_file, _read_meta)
    assert meta["marker"] == "v2", "读中换文件必须丢弃旧结果重读一次"
    assert reads == ["v1", "v2"]


def test_read_stable_raises_after_second_change(slide_file, fake_pairs, monkeypatch):
    """签名持续变化：重试一次后仍变 → SlideFileChanged（fail-closed）。"""
    entry = slide_cache.get_slide("demo.svs", slide_file)
    counter = {"n": 0}
    real_sig = slide_cache.signature_of

    def _always_changing(path):
        counter["n"] += 1
        # 每次 stat 都给出新签名（模拟文件被连续替换）
        return real_sig(path)._replace(st_size=counter["n"])

    monkeypatch.setattr(slide_cache, "signature_of", _always_changing)
    with pytest.raises(slide_cache.SlideFileChanged):
        slide_cache.read_stable(entry, lambda pair: pair["osr"].marker)
    with pytest.raises(slide_cache.SlideFileChanged):
        slide_cache.cached_read_metadata(
            "demo.svs", slide_file, lambda: {"marker": "x"})


# --------------------------------------------------------------------------- #
# 5) evict：快速路径（正确性不依赖它——上面所有用例都未调用 evict）
# --------------------------------------------------------------------------- #
def test_evict_fast_path_closes_handles(slide_file, fake_pairs):
    entry = slide_cache.get_slide("demo.svs", slide_file)
    with slide_cache.borrow_pair(entry) as p:
        pass
    with slide_cache.borrow_pair(entry) as inflight:
        slide_cache.evict("demo.svs")
        assert not inflight["osr"].closed, "在途请求可完成"
    assert inflight["osr"].closed, "evict 后在途句柄归还即关"
    assert p["osr"].closed, "evict 关闭池内空闲句柄"

    # 未 evict 的另一张：照常换代（跨进程语义的本进程等价——
    # share_server 从不 evict，由借用时的签名检查兜底）
    p2 = slide_file.with_name("other.svs")
    p2.write_text("o1", encoding="utf-8")
    entry2 = slide_cache.get_slide("other.svs", p2)
    with slide_cache.borrow_pair(entry2) as op:
        assert op["osr"].marker == "o1"
    _replace_with(p2, "o2")
    with slide_cache.borrow_pair(entry2) as op2:
        assert op2["osr"].marker == "o2"


# --------------------------------------------------------------------------- #
# 6) share_server 路由级：dzi / tile 不跨代命中（假句柄端到端）
# --------------------------------------------------------------------------- #
@pytest.fixture
def share_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "share-data"
    upload_dir = tmp_path / "uploads"
    data_dir.mkdir(parents=True)
    upload_dir.mkdir(parents=True)
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", upload_dir)
    share_store.set_owner_user_id("")
    share = share_store.create_share(["demo.svs"], 24)
    share_srv.app.config["TESTING"] = True
    with share_srv.app.test_client() as c:
        yield c, upload_dir, share["token"]
    # 清分享端瓦片缓存，避免跨用例键残留
    with share_srv._tile_cache_lock:
        share_srv._tile_cache.clear()


def test_share_server_tile_and_dzi_follow_generation(
        share_env, fake_pairs, monkeypatch):
    c, upload_dir, token = share_env
    path = upload_dir / "demo.svs"
    path.write_text("v1", encoding="utf-8")

    tile_url = "/s/%s/api/slide/demo.svs_files/0/0_0.jpeg" % token
    dzi_url = "/s/%s/api/slide/demo.svs.dzi" % token

    r1 = c.get(tile_url)
    assert r1.status_code == 200, r1.get_data(as_text=True)
    body1 = r1.data

    rd1 = c.get(dzi_url)
    assert rd1.status_code == 200

    # 同名重传（保持 mtime+size：mtime-only 缓存必漏，generation 必中）
    _replace_preserving_stat(path, "v2")

    r2 = c.get(tile_url)
    assert r2.status_code == 200
    assert r2.data != body1, "换代后瓦片不得命中旧代 JPEG 缓存"

    # 瓦片缓存键含 generation：旧代条目不再可达
    with share_srv._tile_cache_lock:
        gens = {k[1] for k in share_srv._tile_cache}
    assert len(gens) >= 2, "两代瓦片应各有键：%s" % gens

    # dzi 尺寸走新代句柄（假 dz level_dimensions 固定，只验证不抛错且 200）；
    # 再换一代并立刻请求，验证连续替换下路由不产出旧代内容
    _replace_with(path, "v3")
    r3 = c.get(tile_url)
    assert r3.status_code == 200
    with share_srv._tile_cache_lock:
        assert any(k[1] not in gens for k in share_srv._tile_cache), \
            "第三代理应有独立瓦片缓存键"


def test_share_server_slides_info_follows_signature(share_env, fake_pairs):
    c, upload_dir, token = share_env
    path = upload_dir / "demo.svs"
    path.write_text("v1", encoding="utf-8")
    r1 = c.get("/s/%s/api/slides" % token)
    assert r1.status_code == 200
    items = r1.get_json()
    assert items and items[0]["exists"] is True

    _replace_preserving_stat(path, "v2")
    r2 = c.get("/s/%s/api/slides" % token)
    assert r2.status_code == 200
    assert r2.get_json()[0]["exists"] is True


# --------------------------------------------------------------------------- #
# 7) 字面双进程：分享进程不 evict，管理端（本进程）替换文件后照常换代
#    （G3「app/share_server 两进程都看到新切片」的直接证据；上面各用例是
#    同进程等价，本用例补跨进程形态——子进程模拟 share_server 工作进程）
# --------------------------------------------------------------------------- #
_READER_CHILD = r"""
import os, sys, time, types
from pathlib import Path

# openslide stub（与 tests/_bootstrap 同款；slide_cache import 期需要）
try:
    import openslide  # noqa: F401
except ImportError:
    _o = types.ModuleType("openslide"); _o.OpenSlide = object
    _d = types.ModuleType("openslide.deepzoom"); _d.DeepZoomGenerator = object
    _o.deepzoom = _d
    sys.modules.setdefault("openslide", _o)
    sys.modules.setdefault("openslide.deepzoom", _d)

sys.path.insert(0, %(repo)r)
import slide_cache


class FakeOsr:
    def __init__(self, marker):
        self.marker = marker
        self.closed = False

    def close(self):
        self.closed = True


def fake_make(path, generation):
    return {"osr": FakeOsr(Path(path).read_text(encoding="utf-8")),
            "dz": None, "gen": generation}


slide_cache._make_pair = fake_make

slide, ready, go = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
entry = slide_cache.get_slide("demo.svs", slide)
with slide_cache.borrow_pair(entry) as p1:
    m1, g1 = p1["osr"].marker, p1["gen"]
ready.write_text("1", encoding="utf-8")
deadline = time.time() + 60
while not go.exists() and time.time() < deadline:
    time.sleep(0.05)
# 本进程从未收到 evict：换代只能来自借用时的签名检查
with slide_cache.borrow_pair(entry) as p2:
    m2, g2 = p2["osr"].marker, p2["gen"]
print(repr((m1, g1, m2, g2)))
"""


def test_cross_process_replacement_switches_generation(tmp_path):
    """子进程持有句柄池（不知会 evict）；本进程「保持 mtime+size」同名替换后，
    子进程再次借用必须读到新代（签名只剩 ino 可判别的最难形态）。"""
    import ast
    import subprocess

    slide = tmp_path / "demo.svs"
    slide.write_text("v1", encoding="utf-8")
    ready = tmp_path / "ready"
    go = tmp_path / "go"

    repo = Path(__file__).resolve().parent.parent
    code = _READER_CHILD % {"repo": str(repo)}
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(slide), str(ready), str(go)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 60
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "子进程未能建立首代句柄"
        # 管理端同名重传：保持 mtime+size，只换 inode
        _replace_preserving_stat(slide, "v2")
        go.write_text("1", encoding="utf-8")
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, "reader 子进程失败：%s" % err
    m1, g1, m2, g2 = ast.literal_eval(out.strip())
    assert m1 == "v1"
    assert m2.startswith("v2"), "子进程仍读到旧代内容：%r" % (m2,)
    assert g2 > g1, "子进程未换代：gen %s -> %s" % (g1, g2)
