# -*- coding: utf-8 -*-
"""shares.json fail-closed + 原子写（review 2026-08-29 G1 / DS-1）回归。

覆盖验收矩阵 G1：
- 半截 JSON、顶层错型、关键字段错型 → ShareStoreCorrupt（不回空库），
  原文件 sha256 不变，损坏原文备份到唯一名（时间戳 + sha256 前缀）；
- 备份失败（目录只读）也必须抛原错误，不得回空库；
- EACCES/EIO → ShareStoreUnavailable；
- tmp write/fsync/replace 前后崩溃（failpoint 注入）只留完整旧版或新版，
  无 tmp 残留；
- 两个独立进程并发写 100 轮无丢写（稳定锁文件跨进程互斥）；
- share_server 运行时稳定 503 share_store_corrupt / share_store_unavailable
  （不 404 空库），启动 probe 失败不 ready；
- 空文件 / 文件不存在 → 空库（与 user_store 语义对齐）；
- upload_task_store 复用同一原子写原语（崩溃点同样只见完整旧/新）。

测试全程使用临时目录，不触碰真实 share-data。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import _bootstrap  # noqa: F401  # session 目录+openslide stub（conftest 先行）

sys.path.insert(0, str(Path(__file__).resolve().parent))

import locked_atomic_json  # noqa: E402
import share_store  # noqa: E402
import share_store_json  # noqa: E402
import upload_task_store  # noqa: E402
import share_server as share_srv  # noqa: E402
from pg_compat import json_only  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 基础 fixture：每用例私有数据目录（经 dispatcher 镜像到 json 实现）
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "share-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sf = data_dir / "shares.json"
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", sf)
    assert share_store_json.SHARE_FILE == sf  # dispatcher 镜像生效
    return sf


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


CORRUPT_SAMPLES = {
    "半截 JSON": '{"shares": {"tok": ',
    "顶层是数组": '[1, 2, 3]',
    "顶层是字符串": '"not an object"',
    "非法 UTF-8": b'\xff\xfe{"shares": {}}'.decode("utf-8", "surrogateescape"),
    "关键字段错型 shares": '{"shares": [], "rois": []}',
    "关键字段错型 rois": '{"shares": {}, "rois": {}}',
    "关键字段错型 grants": '{"shares": {}, "grants": {"a": 1}}',
}


@json_only  # shares.json 文件语义；PG 后端无该文件
@pytest.mark.parametrize("label", sorted(CORRUPT_SAMPLES))
def test_corrupt_shares_fails_closed_no_overwrite(isolated_store, label):
    """非空损坏 → ShareStoreCorrupt；原文件字节不变；备份为唯一名。"""
    raw = CORRUPT_SAMPLES[label]
    isolated_store.write_text(raw, encoding="utf-8", errors="surrogateescape")
    before = _sha256(isolated_store)

    for op in (lambda: share_store.get_share("tok_x"),
               lambda: share_store.list_shares(),
               lambda: share_store.probe_readable(),
               lambda: share_store.create_share(["a.svs"], 1)):
        with pytest.raises(share_store.ShareStoreCorrupt):
            op()

    assert _sha256(isolated_store) == before, "损坏文件不得被改写/覆盖"

    # 备份：唯一名（时间戳 + 原文 sha256 前缀），内容与原文一致
    baks = list(isolated_store.parent.glob("shares.json.corrupt-*.bak"))
    assert baks, "损坏后应产生备份"
    for b in baks:
        assert re.fullmatch(
            r"shares\.json\.corrupt-\d{8}T\d{6}-[0-9a-f]{12}\.bak", b.name), b.name
        assert hashlib.sha256(b.read_bytes()).hexdigest() == before


@json_only
def test_corrupt_backup_names_unique_across_content(isolated_store):
    """不同损坏内容的备份互不覆盖（时间戳 + 内容 hash 命名）。"""
    isolated_store.write_text("{first", encoding="utf-8")
    with pytest.raises(share_store.ShareStoreCorrupt):
        share_store.list_shares()
    first_baks = set(isolated_store.parent.glob("shares.json.corrupt-*.bak"))
    assert first_baks

    isolated_store.write_text("{second-different", encoding="utf-8")
    with pytest.raises(share_store.ShareStoreCorrupt):
        share_store.list_shares()
    second_baks = set(isolated_store.parent.glob("shares.json.corrupt-*.bak"))
    assert second_baks > first_baks, "不同内容应有各自唯一备份名"
    assert len(second_baks) == len(first_baks) + 1


@json_only
def test_corrupt_backup_failure_still_raises(isolated_store):
    """备份写不进去（目录只读）时也必须抛 ShareStoreCorrupt，不得回空库。"""
    isolated_store.write_text("{not-json", encoding="utf-8")
    before = _sha256(isolated_store)
    # 锁文件先建好（打开既有文件不需要目录写权限），使锁定阶段不受目录只读影响
    lock = isolated_store.with_name("shares.json.lock")
    lock.write_text("", encoding="utf-8")
    os.chmod(isolated_store.parent, 0o500)
    try:
        with pytest.raises(share_store.ShareStoreCorrupt):
            share_store.list_shares()
        assert _sha256(isolated_store) == before
    finally:
        os.chmod(isolated_store.parent, 0o700)


@json_only
def test_eacces_maps_to_unavailable(isolated_store):
    """数据文件不可读（EACCES）→ ShareStoreUnavailable（与 Corrupt 分流）。"""
    isolated_store.write_text('{"shares": {}}', encoding="utf-8")
    os.chmod(isolated_store, 0o000)
    try:
        with pytest.raises(share_store.ShareStoreUnavailable):
            share_store.list_shares()
    finally:
        os.chmod(isolated_store, 0o600)


@json_only
def test_eio_fault_injection_maps_to_unavailable(isolated_store, monkeypatch):
    """读路径 EIO 注入 → ShareStoreUnavailable。"""
    isolated_store.write_text('{"shares": {}}', encoding="utf-8")

    def _eio(self):
        raise OSError(5, "模拟 I/O 错误")

    monkeypatch.setattr(locked_atomic_json.LockedFileSession, "read_bytes", _eio)
    with pytest.raises(share_store.ShareStoreUnavailable):
        share_store.list_shares()
    with pytest.raises(share_store.ShareStoreUnavailable):
        share_store.probe_readable()


@json_only
def test_missing_and_empty_file_are_empty_store(isolated_store):
    """文件不存在 / 零字节 → 空库（与 user_store 对齐），probe 通过。"""
    assert not isolated_store.exists()
    assert share_store.list_shares() == []
    assert share_store.probe_readable() is True

    isolated_store.write_bytes(b"")
    assert share_store.list_shares() == []
    assert share_store.get_share("nope") is None
    assert share_store.probe_readable() is True


@json_only
def test_valid_store_roundtrip_and_lock_file(isolated_store):
    """正常路径：写读通；锁迁到独立稳定锁文件（不再锁数据 inode）。"""
    tok = share_store.create_share(["a.svs"], 1)["token"]
    assert share_store.get_share(tok)["token"] == tok
    lock = isolated_store.with_name("shares.json.lock")
    assert lock.is_file(), "应使用独立稳定锁文件 shares.json.lock"
    # 数据文件存在且为合法 JSON（原子替换产物）
    json.loads(isolated_store.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 原子写 failpoint：崩溃注入后只见完整旧版或新版
# --------------------------------------------------------------------------- #
OLD = {"shares": {"tok_old": {"slides": ["old.svs"], "created_at": 1.0,
                              "expires_at": 1e12, "revoked": False}},
       "rois": [], "projects": {}, "slide_meta": {}, "change_seq_by_slide": {},
       "grants": [], "comments": [], "audit": [], "plugin_installations": [],
       "run_grants": []}


def _write_valid(path: Path, marker: str) -> None:
    data = json.loads(json.dumps(OLD))
    data["shares"]["tok_old"]["slides"] = [marker]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")


@json_only
@pytest.mark.parametrize("stage", ["after_write", "after_fsync",
                                   "before_replace", "after_replace"])
def test_atomic_write_failpoint_keeps_intact_version(isolated_store, monkeypatch, stage):
    """tmp 写入/fsync/replace 各崩溃点：数据文件只保留完整旧版或新版。"""
    _write_valid(isolated_store, "old-version")
    old_sha = _sha256(isolated_store)

    def _boom(name):
        if name == stage:
            raise RuntimeError("模拟崩溃于 %s" % stage)

    monkeypatch.setattr(locked_atomic_json, "failpoint", _boom)
    with pytest.raises(RuntimeError):
        share_store.create_share(["b.svs"], 1)

    raw = isolated_store.read_bytes()
    # 只能是完整旧版或完整新版（可解析 + 结构合法）
    data = json.loads(raw)
    assert isinstance(data, dict) and "shares" in data
    now_sha = hashlib.sha256(raw).hexdigest()
    if stage == "after_replace":
        assert now_sha != old_sha, "replace 已发生后应是新版"
    else:
        assert now_sha == old_sha, "replace 前崩溃必须保留完整旧版"
    # 无 tmp 残留
    leftovers = [p for p in isolated_store.parent.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == [], "崩溃路径应清理 tmp 文件"


@json_only
@pytest.mark.parametrize("stage", ["after_write", "before_replace"])
def test_upload_task_store_shares_atomic_primitive(tmp_path, monkeypatch, stage):
    """upload_tasks.json 复用同一原语：崩溃点后文件仍可解析（完整旧/新）。"""
    f = tmp_path / "upload_tasks.json"
    monkeypatch.setattr(upload_task_store, "UPLOAD_TASK_FILE", f)
    task = upload_task_store.create_task("u1", "a.svs", "a.svs", 100, 100)
    upload_task_store.append_chunk(task["upload_id"], 0, 100, "deadbeef")
    before = _sha256(f)

    def _boom(name):
        if name == stage:
            raise RuntimeError("模拟崩溃于 %s" % stage)

    monkeypatch.setattr(locked_atomic_json, "failpoint", _boom)
    with pytest.raises(RuntimeError):
        upload_task_store.begin_commit(task["upload_id"])

    data = json.loads(f.read_text(encoding="utf-8"))
    assert isinstance(data.get("tasks"), dict)
    if stage == "before_replace":
        assert _sha256(f) == before
    # 锁文件与数据文件分离
    assert (tmp_path / "upload_tasks.json.lock").is_file()


# --------------------------------------------------------------------------- #
# 双进程并发写：稳定锁跨进程互斥，100 轮无丢写
# --------------------------------------------------------------------------- #
_WORKER = r"""
import os, sys
sys.path.insert(0, %(repo)r)
import share_store
n = 0
for i in range(50):
    s = share_store.create_share(["p.svs"], 24, creator_user_id="pid-%%d" %% os.getpid())
    assert s and s.get("token")
    n += 1
print(n)
"""


@json_only
def test_two_processes_concurrent_writes_no_loss(tmp_path, monkeypatch):
    """两个独立进程各写 50 轮：最终恰好 100 个分享，无丢写。"""
    data_dir = tmp_path / "share-data"
    data_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["SHARE_DATA_DIR"] = str(data_dir)
    env.pop("STORAGE_BACKEND", None)
    code = _WORKER % {"repo": str(REPO_ROOT)}

    procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
             for _ in range(2)]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, "worker 失败：%s" % err
        outs.append(out.strip())
    assert outs == ["50", "50"]

    # 校验方：本进程以同一数据目录读回（经 dispatcher 镜像）
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    shares = share_store.list_shares()
    tokens = [s["token"] for s in shares]
    assert len(tokens) == 100, "并发写丢失：%d/100" % len(tokens)
    assert len(set(tokens)) == 100, "token 重复（互斥失效）"
    json.loads((data_dir / "shares.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# share_server：运行时 503 + 启动 probe
# --------------------------------------------------------------------------- #
@json_only
def test_share_server_corrupt_returns_503_not_404(tmp_path, monkeypatch):
    """损坏库上分享路由稳定 503 share_store_corrupt，绝不 404 空库。"""
    data_dir = tmp_path / "share-data"
    data_dir.mkdir(parents=True)
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setenv("SHARE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", data_dir / "shares.json")
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", upload_dir)
    share_store.set_owner_user_id("")
    # 先建一个有效分享，再把文件写坏（模拟运行中损坏）
    tok = share_store.create_share(["a.svs"], 24)["token"]
    (data_dir / "shares.json").write_text('{"shares": {"tok"', encoding="utf-8")

    share_srv.app.config["TESTING"] = True
    c = share_srv.app.test_client()
    for url in ("/s/%s" % tok,
                "/s/%s/api/config" % tok,
                "/s/%s/api/slides" % tok,
                "/s/%s/api/rois" % tok):
        r = c.get(url)
        assert r.status_code == 503, "%s 应 503，实际 %s" % (url, r.status_code)
        assert r.get_json()["error"] == "share_store_corrupt"
    # 健康探针同样 503
    r = c.get("/s/healthz")
    assert r.status_code == 503
    assert r.get_json()["error"] == "share_store_corrupt"


@json_only
def test_share_server_unavailable_returns_503(tmp_path, monkeypatch):
    """EACCES（数据文件不可读）→ 503 share_store_unavailable。"""
    data_dir = tmp_path / "share-data"
    data_dir.mkdir(parents=True)
    sf = data_dir / "shares.json"
    sf.write_text('{"shares": {}}', encoding="utf-8")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", sf)
    monkeypatch.setattr(share_srv, "UPLOAD_DIR", tmp_path / "uploads")

    os.chmod(sf, 0o000)
    try:
        share_srv.app.config["TESTING"] = True
        c = share_srv.app.test_client()
        r = c.get("/s/healthz")
        assert r.status_code == 503
        assert r.get_json()["error"] == "share_store_unavailable"
    finally:
        os.chmod(sf, 0o600)


@json_only
def test_startup_probe_fails_not_ready(tmp_path, monkeypatch):
    """启动 probe：损坏库上 SystemExit（worker 不 ready），恢复后通过。"""
    data_dir = tmp_path / "share-data"
    data_dir.mkdir(parents=True)
    sf = data_dir / "shares.json"
    sf.write_text("[broken", encoding="utf-8")
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", sf)

    with pytest.raises(SystemExit):
        share_srv.run_share_store_startup_probe()
    assert share_srv.SHARE_STORE_STARTUP_ERROR

    sf.write_text('{"shares": {}}', encoding="utf-8")
    assert share_srv.run_share_store_startup_probe() is True
