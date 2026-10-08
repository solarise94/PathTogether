# -*- coding: utf-8 -*-
"""P0-A §3.4 ZIP 解压防护测试（docs/open-registration-security-remediation §6.2；
P4-app 断言换新：解包落 staging + 逐逻辑切片统一发布）。

覆盖：
  - 合法 bundle 仍可发布：单文件切片、MRXS + 同 stem 伴侣目录（多层包装
    剥层）——产物落 objects/<slide_id>/（入口 data.<ext>，伴侣 data/…）；
  - 拒绝且无残留：声明大小正常但实际流超限（防实现层偏差的兜底）、极高
    压缩比、过多成员、深目录、重复规范化路径（大小写不敏感）、symlink、
    设备成员、加密成员、混入无关顶层文件/目录、zip-slip 回归；
  - 同名平铺文件不再构成冲突（P4-app：目标冲突预检拆除——每 item 预分配
    slide_id，objects/<sid> 唯一天然无冲突）；
  - 磁盘水位在解压过程中触发（507）；
  - PG：展开总量超过预占 → 原子 topup；quota 不足 → 413。

直接调 _prepare_zip_bundle + 逐 item 统一发布（G7：生产端点在两阶段之间
插入 task intent = upload_task_store.begin_legacy_commit，端点级流程在
test_upload_guard.py / test_upload_accounting_recovery.py / 
test_zip_slide_id_pg.py 覆盖）。
"""
import io
import os
import stat as stat_mod
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import pytest  # noqa: E402

import share_store  # noqa: E402
import user_store  # noqa: E402
import upload_guard  # noqa: E402
import slide_io  # noqa: E402
import upload_content  # noqa: E402  # U5：zip 服务直驱（app 壳已随检查点 B 删除）
from _pt_helpers import (
    isolate_app,
    clear_upload_dir,
)  # noqa: E402


SVS = b"fake-svs-content-0123456789"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """独立存储 + ZIP 上限复位 + _validate_slide_file 放行 + 清空 uploads。

    P4-app：发布管线 allocate_slide 需要非空 owner——本地免认证态注入配置
    owner（p3-local-owner 同款口径）。"""
    isolate_app(monkeypatch, tmp_path, UPLOAD_DIR)
    import share_store as _ss
    _ss.set_owner_user_id(
        user_store.create_user("p3-local-owner@x.com",
                               "localownerpass12345", role="user")["user_id"])
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 0)
    monkeypatch.setattr(upload_content, "ZIP_MAX_MEMBERS", 4096)
    monkeypatch.setattr(upload_content, "ZIP_MAX_PATH_DEPTH", 8)
    monkeypatch.setattr(upload_content, "ZIP_MAX_MEMBER_BYTES",
                        upload_guard.UPLOAD_MAX_REQUEST_BYTES)
    monkeypatch.setattr(upload_content, "ZIP_MAX_TOTAL_BYTES",
                        2 * upload_guard.UPLOAD_MAX_REQUEST_BYTES)
    monkeypatch.setattr(upload_content, "ZIP_MAX_COMPRESSION_RATIO", 100.0)
    # A0 异常契约：放行 stub 返回 None，签名兼容 format_hint 关键字
    monkeypatch.setattr(upload_content, "validate_slide_file",
                        lambda p, **_: None)
    clear_upload_dir(UPLOAD_DIR)
    yield


def _make_zip(members, path=None, compression=zipfile.ZIP_DEFLATED):
    """members: [(name, bytes | ZipInfo-paired bytes)]；返回 zip 文件 Path。"""
    path = Path(path or (Path(UPLOAD_DIR) / "test-input.zip"))
    with zipfile.ZipFile(path, "w", compression) as zf:
        for entry in members:
            name, data = entry
            zf.writestr(name, data)
    return path


def _make_zip_raw(infos, path=None):
    """infos: [ZipInfo]（external_attr/flag_bits 等由调用方定制）。"""
    path = Path(path or (Path(UPLOAD_DIR) / "test-input.zip"))
    with zipfile.ZipFile(path, "w") as zf:
        for zinfo, data in infos:
            zf.writestr(zinfo, data)
    return path


def _extract_and_promote(z, reservation=None):
    """P4-app 生产管线聚合器：prepare → 受理（allocate+bind+intent 同一
    事务）→ 逐 item 统一发布 → finish_commit。

    成功返回 (main, extracted, info)——info 含 slide_ids/objects 目录映射；
    失败返回 (msg, status)（保留旧两段式契约）。"""
    import pg_store
    import slide_publish
    import slide_store
    import slide_storage
    import upload_task_store

    result = upload_content.prepare_zip_bundle(
        z, reservation=reservation, upload_root=UPLOAD_DIR)
    if isinstance(result, tuple):
        return result
    bundle = result
    owner = (share_store.get_owner_user_id() or "").strip()
    upload_id = upload_task_store.new_task_id()
    artifacts = upload_content.zip_build_artifacts(bundle)
    try:
        import psycopg.rows
        conn = pg_store.connect()
        conn.row_factory = psycopg.rows.dict_row
        try:
            with pg_store.transaction(conn):
                for item in bundle["items"]:
                    desc = slide_store.allocate_slide(
                        owner, original_filename=Path(item["key"]).name,
                        format_ext=item["ext"], conn=conn)
                    upload_task_store.bind_upload_task_item(
                        conn, upload_id, item["key"], desc.slide_id)
                _uid, token, task = upload_task_store.begin_legacy_commit(
                    owner_user_id=owner, filename="t.zip",
                    safe_name=Path(bundle["main"]).name,
                    artifacts=artifacts,
                    reservation_id=(reservation or {}).get("reservation_id"),
                    upload_id=upload_id, conn=conn)
        finally:
            conn.close()
        items = upload_task_store.list_upload_task_items(upload_id)
        plans = upload_content.zip_item_plans(artifacts, items)
        published, _failures, _settled = upload_content.zip_publish_items(
            upload_id, token, plans, owner,
            extract_dir=bundle["extract_dir"],
            upload_root=UPLOAD_DIR)
        upload_task_store.finish_commit(
            upload_id, token, upload_content.manifest_sha(artifacts),
            settle_bytes=int(published))
    finally:
        slide_storage.remove_staging_tree(upload_id,
                                          root=UPLOAD_DIR)
    slide_ids = {r["item_key"]: r["slide_id"]
                 for r in upload_task_store.list_upload_task_items(upload_id)}
    info = {"slide_ids": slide_ids,
            "objects": {k: slide_storage.bundle_dir(v,
                                                    root=UPLOAD_DIR)
                        for k, v in slide_ids.items()}}
    return bundle["main"], [i["key"] for i in bundle["items"]], info


def _residue():
    return [p.name for p in Path(UPLOAD_DIR).iterdir()
            if p.name.startswith(".uploading-")
            or p.name.startswith(".extracting-")]


def _no_slide_residue(*names):
    """断言：无临时残留，且指定文件都不在 UPLOAD_DIR。"""
    assert _residue() == []
    for n in names:
        assert not (Path(UPLOAD_DIR) / n).exists(), n


# =========================================================================== #
# 1. 合法 bundle 仍可上传（docs §3.4：不能误伤 MRXS 伴侣目录语义）
# =========================================================================== #
def test_legal_single_file_zip():
    z = _make_zip([("a.svs", SVS)])
    result = _extract_and_promote(z)
    assert not isinstance(result[1], int), result
    main, extracted, info = result
    assert main == "a.svs"
    assert extracted == ["a.svs"]
    sid = info["slide_ids"]["a.svs"]
    assert (info["objects"]["a.svs"] / "data.svs").read_bytes() == SVS
    assert not (Path(UPLOAD_DIR) / "a.svs").exists()  # 不再平铺
    assert _residue() == []


def test_legal_mrxs_with_companion_dir():
    z = _make_zip([
        ("S.mrxs", b"mrxs-main"),
        ("S/", b""),
        ("S/Slidedat.ini", b"ini"),
        ("S/Level_0/data.dat", b"dat"),
    ])
    main, extracted, info = _extract_and_promote(z)
    # 返回契约：extracted 只列有效切片（逻辑 item）；伴侣目录文件**同包**
    # 发布（objects/<sid>/data/… 保留包内相对关系——P4-app §2.5）
    assert main == "S.mrxs"
    assert extracted == ["S.mrxs"]
    obj = info["objects"]["S.mrxs"]
    assert (obj / "data.mrxs").read_bytes() == b"mrxs-main"
    assert (obj / "data" / "Slidedat.ini").read_bytes() == b"ini"
    assert (obj / "data" / "Level_0" / "data.dat").read_bytes() == b"dat"
    assert not (Path(UPLOAD_DIR) / "S.mrxs").exists()
    assert not (Path(UPLOAD_DIR) / "S").exists()
    assert _residue() == []


def test_legal_multi_layer_wrapped_zip():
    """文件夹打包产生的多层包装：逐层剥掉后按顶层 bundle 识别。"""
    z = _make_zip([("wrap/inner/S.mrxs", b"m"), ("wrap/inner/S/d.dat", b"d")])
    main, extracted, info = _extract_and_promote(z)
    assert main == "S.mrxs"
    assert extracted == ["S.mrxs"]
    assert (info["objects"]["S.mrxs"] / "data" / "d.dat").read_bytes() == b"d"


def test_legal_multiple_single_file_slides():
    z = _make_zip([("a.svs", SVS), ("b.tif", b"tif-bytes")])
    main, extracted, info = _extract_and_promote(z)
    assert set(extracted) == {"a.svs", "b.tif"}
    # 多逻辑切片各得各 slide_id/objects 目录（P4-app §2.2）
    assert len(set(info["slide_ids"].values())) == 2
    assert (info["objects"]["a.svs"] / "data.svs").read_bytes() == SVS
    assert (info["objects"]["b.tif"] / "data.tif").read_bytes() == b"tif-bytes"


# =========================================================================== #
# 2. 解压炸弹 / 结构攻击：拒绝且无残留
# =========================================================================== #
def test_declared_member_size_over_limit(monkeypatch):
    """随机数据（压缩比≈1）超单成员上限 → 拒绝（不依赖压缩比触发）。"""
    monkeypatch.setattr(upload_content, "ZIP_MAX_MEMBER_BYTES", 1000)
    z = _make_zip([("a.svs", os.urandom(5000))])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "大小上限" in msg
    _no_slide_residue("a.svs")


def test_total_expansion_over_limit(monkeypatch):
    monkeypatch.setattr(upload_content, "ZIP_MAX_MEMBER_BYTES", 10 ** 9)
    monkeypatch.setattr(upload_content, "ZIP_MAX_TOTAL_BYTES", 2000)
    z = _make_zip([("a.svs", os.urandom(1200)), ("b.svs", os.urandom(1200))])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "总展开量" in msg
    _no_slide_residue("a.svs", "b.svs")


def test_extreme_compression_ratio_rejected():
    """全零数据（deflate 后极小）→ 声明/实际都在限内但压缩比异常 → 拒绝。"""
    z = _make_zip([("a.svs", b"\x00" * (1024 * 1024))])  # 1 MiB 零 → 比例 >1000
    msg, status = _extract_and_promote(z)
    assert status == 400 and "压缩比" in msg
    _no_slide_residue("a.svs")


def test_too_many_members(monkeypatch):
    monkeypatch.setattr(upload_content, "ZIP_MAX_MEMBERS", 5)
    z = _make_zip([("S.mrxs", b"m")] + [("S/f%d.dat" % i, b"d") for i in range(6)])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "成员数" in msg
    _no_slide_residue("S.mrxs")


def test_deep_path_rejected():
    z = _make_zip([("a/b/c/d/e/f/g/h/i.svs", SVS)])  # 深度 9 > 8
    msg, status = _extract_and_promote(z)
    assert status == 400 and "路径深度" in msg
    _no_slide_residue("a")


def test_duplicate_normalized_path_rejected():
    """大小写不敏感文件系统上的覆盖攻击：大小写变体也算重复。"""
    z = _make_zip([("S/a.dat", b"1"), ("S/A.dat", b"2"), ("S.mrxs", b"m")])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "重复路径" in msg
    _no_slide_residue("S.mrxs", "S")


def test_exact_duplicate_path_rejected():
    with pytest.warns(UserWarning, match="Duplicate name"):
        z = _make_zip([("a.svs", SVS), ("a.svs", SVS)])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "重复路径" in msg
    _no_slide_residue("a.svs")


def test_symlink_member_rejected():
    zi = zipfile.ZipInfo("link.svs")
    zi.create_system = 3
    zi.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
    z = _make_zip_raw([(zi, b"a.svs")])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "非法成员类型" in msg
    _no_slide_residue("link.svs", "a.svs")


def test_device_member_rejected():
    zi = zipfile.ZipInfo("dev.svs")
    zi.create_system = 3
    zi.external_attr = (stat_mod.S_IFCHR | 0o644) << 16
    z = _make_zip_raw([(zi, b"")])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "非法成员类型" in msg


def test_encrypted_member_rejected():
    """加密成员（中央目录声明加密标志）→ 入口即拒，不尝试打开/解密。

    zipfile 写入时会剥掉 flag_bits 的加密位，无法直接构造；这里在读取侧
    把 infolist 声明改为带 0x1 标志（等价于恶意/加密 zip 的元数据形态），
    防护在 zf.open 之前生效。
    """
    import unittest.mock as mock

    z = _make_zip([("a.svs", SVS)])
    real_infolist = zipfile.ZipFile.infolist

    def _encrypted_infolist(self):
        infos = real_infolist(self)
        for i in infos:
            i.flag_bits |= 0x1
        return infos

    with mock.patch.object(zipfile.ZipFile, "infolist", _encrypted_infolist):
        msg, status = _extract_and_promote(z)
    assert status == 400 and "加密成员" in msg
    _no_slide_residue("a.svs")


def test_unrelated_toplevel_file_rejected():
    z = _make_zip([("a.svs", SVS), ("readme.txt", b"unrelated")])
    msg, status = _extract_and_promote(z)
    assert status == 400
    _no_slide_residue("a.svs", "readme.txt")


def test_unrelated_toplevel_dir_rejected():
    z = _make_zip([("a.svs", SVS), ("other/x.bin", b"unrelated")])
    msg, status = _extract_and_promote(z)
    assert status == 400
    _no_slide_residue("a.svs", "other")


def test_no_slide_in_zip_rejected():
    z = _make_zip([("data/x.bin", b"nope")])
    msg, status = _extract_and_promote(z)
    assert status == 400
    _no_slide_residue("data")


def test_zip_slip_regression():
    """绝对路径 member → 「非法路径」拒绝；.. member 被跳过（绝不落盘父目录）。"""
    z = _make_zip([("/abs/evil.svs", SVS)])
    msg, status = _extract_and_promote(z)
    assert status == 400 and "非法路径" in msg
    _no_slide_residue("abs")
    # ".." member：隐藏文件过滤先行跳过（既有行为），任何情况下不得写出
    z2 = _make_zip([("../evil.svs", SVS)])
    msg2, status2 = _extract_and_promote(z2)
    assert status2 == 400
    _no_slide_residue("evil.svs")
    assert not (Path(UPLOAD_DIR).parent / "evil.svs").exists()


def test_actual_stream_exceeds_declared_backstop(monkeypatch):
    """声明大小正常、实际流超限（模拟实现层偏差/恶意本地头）：计数兜底中止。

    stdlib zipfile 按声明值截断（实际不可能超），本用例注入一个比声明更大
    的流验证第二道计数确实独立生效。
    """
    z = _make_zip([("a.svs", b"tiny")])  # 声明 4 字节

    class _FatStream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b"Z" * 4096 if n and n > 0 else b"Z" * 4096

    import unittest.mock as mock
    with mock.patch.object(zipfile.ZipFile, "open",
                           lambda self, name, mode="r", pwd=None: _FatStream()):
        msg, status = _extract_and_promote(z)
    assert status == 400 and "实际展开量" in msg
    _no_slide_residue("a.svs")


def test_watermark_during_extraction_507(monkeypatch):
    """解压过程中的水位检查：首块即查（检查粒度压到 1 字节）。"""
    monkeypatch.setattr(upload_content, "ZIP_WATERMARK_CHECK_BYTES", 1)
    monkeypatch.setattr(upload_guard, "UPLOAD_RESERVED_FREE_BYTES", 10 ** 15)
    z = _make_zip([("a.svs", SVS)])
    msg, status = _extract_and_promote(z)
    assert status == 507
    _no_slide_residue("a.svs")


# =========================================================================== #
# 3. 目标冲突预检拆除（P4-app §2.6）：同名平铺文件不再构成冲突
# =========================================================================== #
def test_same_name_flat_file_no_longer_conflicts():
    """旧「名称不可用 409」拆除：每 item 预分配 slide_id，objects/<sid>
    唯一天然无冲突——同名平铺 legacy 文件与发布产物互不影响、不覆盖。"""
    (Path(UPLOAD_DIR) / "a.svs").write_bytes(b"existing")  # 可能是其他用户的
    z = _make_zip([("a.svs", SVS)])
    main, extracted, info = _extract_and_promote(z)
    assert main == "a.svs"
    assert (Path(UPLOAD_DIR) / "a.svs").read_bytes() == b"existing"  # 未覆盖
    assert (info["objects"]["a.svs"] / "data.svs").read_bytes() == SVS
    assert _residue() == []


# =========================================================================== #
# 4. PG：展开总量超过预占 → 原子 topup；quota 不足 → 413 且清理
# =========================================================================== #
import psycopg

def _set_quota(user_id, quota_bytes):
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO upload_user_quotas (user_id, quota_bytes) "
                "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
                "SET quota_bytes = EXCLUDED.quota_bytes",
                (user_id, quota_bytes))


def test_zip_expansion_topup_success():
    """zip 体小（CL 小）但展开大：受理前 topup 补占；任务 finish_commit 按
    全部已发布 item 字节合计一次性 consume（P4-app §2.4 口径）。"""
    uid = user_store.create_user("z1@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 10 ** 6)
    r = upload_guard.reserve_upload(uid, 100)  # CL 提示值远小于展开量
    # 随机数据：压缩比 ≈1，避免触发压缩比闸（本用例只测 topup 路径）
    z = _make_zip([("S.mrxs", b"m"), ("S/d.dat", os.urandom(5000))])
    result = _extract_and_promote(z, reservation=r)
    assert not isinstance(result[1], int), result
    main, extracted, info = result
    # R16 替代原合同「topup 后预占 = 实际展开总量」：解压前补占到峰值——
    # 压缩源与展开文件（1 字节 mrxs + 5000 字节 dat）同时在盘上。
    refreshed = upload_guard.get_reservation(r["reservation_id"])
    assert refreshed["reserved_bytes"] == z.stat().st_size + 1 + 5000
    row = upload_guard.get_quota_row(uid)
    # 任务收口 consume 已在聚合器内发生（一次性结算，无残留预占）
    assert row["used_bytes"] == 1 + 5000 and row["reserved_bytes"] == 0
    assert (info["objects"]["S.mrxs"] / "data.mrxs").exists()


def test_zip_expansion_topup_quota_exceeded():
    uid = user_store.create_user("z2@x.com", "pass1234pass1234", role="user")["user_id"]
    _set_quota(uid, 3000)  # 展开总量 5005 超配额
    r = upload_guard.reserve_upload(uid, 100)
    z = _make_zip([("S.mrxs", b"m"), ("S/d.dat", os.urandom(5000))])
    msg, status = _extract_and_promote(z, reservation=r)
    assert status == 413
    _no_slide_residue("S.mrxs", "S")
    upload_guard.release_reservation(r["reservation_id"])
    assert upload_guard.get_quota_row(uid)["reserved_bytes"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
