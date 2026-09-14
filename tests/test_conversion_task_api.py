# -*- coding: utf-8 -*-
"""W4：转换任务列表/重试（C01，store 级）+ 格式目录（public_catalog）。

列表/分页/重试先在 store 层锁行为（HTTP 路由接线由 app.py 后续批次补）；
conversion_http 纯函数（无 Flask 路由）在此直接调用覆盖错误码翻译。
conftest 已起内嵌 PG 并应用全部迁移（ensure_schema），每用例前
TRUNCATE conversion_jobs / conversion_job_sources 保证隔离。

运行：cd 项目根 && .venv/bin/python -m pytest tests/test_conversion_task_api.py -q
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402,F401

import pytest  # noqa: E402

import conversion_http  # noqa: E402
import conversion_store  # noqa: E402
import pg_store  # noqa: E402
import slide_format_registry as reg  # noqa: E402


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _sha(name):
    return hashlib.sha256(("sha:" + name).encode("utf-8")).hexdigest()


def _mk_job(owner, name, *, source_format="kfb_bf_v1", canonical=None):
    """owner + 唯一 source_sha256 → 新建 queued 任务（幂等键不撞车）。"""
    return conversion_store.create_job(
        owner_user_id=owner,
        upload_id="up_" + name,
        source_name=name,
        source_sha256=_sha(owner + "/" + name),
        source_format=source_format,
        canonical_name=canonical or (name.rsplit(".", 1)[0] + ".tif"))


def _fail_first(name="f.kfb", owner="userA", worker="w1"):
    """新建一个任务并置为 failed（claim_one 全局取最旧，先建即目标）。"""
    job = _mk_job(owner, name)
    claimed = conversion_store.claim_one(worker)
    assert claimed is not None and claimed["id"] == job["id"]
    return conversion_store.fail_job(claimed["id"], worker, "kfb_bad",
                                     detail="internal-secret-detail")


def _ids(page):
    return [i["conversion_job_id"] for i in page["items"]]


# --------------------------------------------------------------------------- #
# C01：list_jobs（owner 隔离 / 分页 / group / 空串 owner）
# --------------------------------------------------------------------------- #
def test_list_jobs_owner_isolation():
    a1 = _mk_job("userA", "a1.kfb")
    a2 = _mk_job("userA", "a2.kfb")
    b1 = _mk_job("userB", "b1.kfb")

    page_a = conversion_store.list_jobs(owner_user_id="userA")
    assert set(_ids(page_a)) == {a1["id"], a2["id"]}
    assert "userB" not in json.dumps(page_a)

    page_b = conversion_store.list_jobs(owner_user_id="userB")
    assert _ids(page_b) == [b1["id"]]

    # 不存在 / 其他 owner 的任务绝不出现
    assert b1["id"] not in _ids(page_a)


def test_list_jobs_empty_string_is_valid_owner():
    e = _mk_job("", "empty-owner.kfb")
    assert e["owner_user_id"] == ""
    a = _mk_job("userA", "a.kfb")

    page = conversion_store.list_jobs(owner_user_id="")
    assert _ids(page) == [e["id"]]
    # 空串 owner 与具名 owner 互不可见（等值过滤，不归一）
    assert _ids(conversion_store.list_jobs(owner_user_id="userA")) == [a["id"]]


def test_list_jobs_pagination_cursor_no_dup_no_missing():
    jobs = [_mk_job("userA", "p%d.kfb" % i) for i in range(3)]

    seen = []
    cursor = None
    for _ in range(5):  # 游标走尽为止（防止死循环上限）
        page = conversion_store.list_jobs(owner_user_id="userA", limit=2,
                                          cursor=cursor)
        seen.extend(_ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert len(seen) == len(set(seen))          # 无重复
    assert set(seen) == {j["id"] for j in jobs}  # 无漏项

    # 首页：最新在前（created_at DESC, id DESC 稳定序）
    first = conversion_store.list_jobs(owner_user_id="userA", limit=2)
    assert len(first["items"]) == 2
    assert first["next_cursor"]                 # 还有下一页
    all_items = first["items"] + conversion_store.list_jobs(
        owner_user_id="userA", cursor=first["next_cursor"])["items"]
    created = [i["created_at"] for i in all_items]
    assert created == sorted(created, reverse=True)


def test_list_jobs_limit_clamp():
    for i in range(3):
        _mk_job("userA", "c%d.kfb" % i)
    # 0/负数 → 1；>100 → 100（此处仅验证不报错且 ≤ 上限）
    assert len(conversion_store.list_jobs(
        owner_user_id="userA", limit=0)["items"]) == 1
    page = conversion_store.list_jobs(owner_user_id="userA", limit=9999)
    assert len(page["items"]) == 3
    assert page["next_cursor"] is None


def test_list_jobs_bad_group_and_cursor():
    with pytest.raises(ValueError):
        conversion_store.list_jobs(owner_user_id="userA", group="all")
    with pytest.raises(ValueError):
        conversion_store.list_jobs(owner_user_id="userA", cursor="!!!not-b64")


def test_list_jobs_group_open_vs_recent():
    failed = _fail_first()                       # 最旧 → failed（finished_at=now）
    ready_src = _mk_job("userA", "r.kfb")
    claimed = conversion_store.claim_one("w1")
    assert claimed["id"] == ready_src["id"]
    conversion_store.complete_job(
        ready_src["id"], "w1", ready_src["canonical_name"],
        owner_user_id="userA", settle_bytes=1024)
    still_open = _mk_job("userA", "open.kfb")    # 领取后仍 queued

    open_page = conversion_store.list_jobs(owner_user_id="userA",
                                           group="open")
    assert _ids(open_page) == [still_open["id"]]

    recent_page = conversion_store.list_jobs(owner_user_id="userA",
                                             group="recent")
    # recent：近 7 天终态（failed+ready）与全部进行中
    assert set(_ids(recent_page)) == {failed["id"], ready_src["id"],
                                      still_open["id"]}


def test_recent_group_drops_finished_older_than_window():
    old = _fail_first()
    conn = pg_store.connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE conversion_jobs SET finished_at = "
                    "now() - interval '8 days' WHERE id=%s", (old["id"],))
    finally:
        conn.close()

    still_open = _mk_job("userA", "open2.kfb")
    recent = conversion_store.list_jobs(owner_user_id="userA", group="recent")
    assert old["id"] not in _ids(recent)
    assert still_open["id"] in _ids(recent)      # 进行中始终保留


# --------------------------------------------------------------------------- #
# C01：retry_job
# --------------------------------------------------------------------------- #
def test_retry_failed_requeues_same_job():
    failed = _fail_first(name="r1.kfb")
    job_id = failed["id"]
    assert failed["attempt"] == 1

    view = conversion_store.retry_job(job_id, owner_user_id="userA",
                                      source_available=True)
    assert view["conversion_job_id"] == job_id       # 同 id，不建第二个任务
    assert view["state"] == "queued"
    assert view["attempt"] == 2                      # claim +1、requeue +1
    assert view["error_code"] is None

    row = conversion_store.get_job(job_id)
    assert row["state"] == "queued"
    assert row["error_detail_internal"] is None      # 内部细节一并清空
    assert row["finished_at"] is None

    # 重试后进入 open 组
    assert _ids(conversion_store.list_jobs(owner_user_id="userA")) == [job_id]


def test_retry_ready_or_open_conflict():
    # ready → StateConflict
    ready = _mk_job("userA", "ok.kfb")
    claimed = conversion_store.claim_one("w1")
    conversion_store.complete_job(ready["id"], "w1", ready["canonical_name"],
                                  owner_user_id="userA", settle_bytes=512)
    with pytest.raises(conversion_store.StateConflict):
        conversion_store.retry_job(ready["id"], owner_user_id="userA",
                                   source_available=True)

    # 进行中（queued）同样不可重试
    open_job = _mk_job("userA", "open3.kfb")
    with pytest.raises(conversion_store.StateConflict):
        conversion_store.retry_job(open_job["id"], owner_user_id="userA",
                                   source_available=True)


def test_retry_other_users_job_not_found():
    job = _fail_first()   # userA 的失败任务（_mk_job 默认 owner）
    with pytest.raises(conversion_store.JobNotFound):
        conversion_store.retry_job(job["id"], owner_user_id="userB",
                                   source_available=True)
    with pytest.raises(conversion_store.JobNotFound):
        conversion_store.retry_job("cvj_nope", owner_user_id="userA",
                                   source_available=True)


def test_retry_source_unavailable_conflict():
    job = _fail_first()
    with pytest.raises(conversion_store.StateConflict) as ei:
        conversion_store.retry_job(job["id"], owner_user_id="userA",
                                   source_available=False)
    assert "source_unavailable" in str(ei.value)
    # 状态保持 failed，未被翻动
    assert conversion_store.get_job(job["id"])["state"] == "failed"


def test_public_view_never_exposes_internal_detail():
    failed = _fail_first()
    view = conversion_store.public_view(conversion_store.get_job(failed["id"]))
    assert "error_detail_internal" not in view
    assert "internal-secret-detail" not in json.dumps(view)
    # 列表路径同样只走 public_view
    page = conversion_store.list_jobs(owner_user_id="userA", group="recent")
    blob = json.dumps(page)
    assert "error_detail_internal" not in blob
    assert "internal-secret-detail" not in blob


# --------------------------------------------------------------------------- #
# conversion_http 纯函数（无 Flask 路由）：参数映射 + 错误码翻译
# --------------------------------------------------------------------------- #
def test_http_list_visibility_and_validation():
    ident_a = {"user_id": "userA", "role": "user"}
    job = _mk_job("userA", "h1.kfb")

    body, status = conversion_http.handle_list(ident_a, {})
    assert status == 200
    assert _ids(body) == [job["id"]]
    assert body["group"] == "open"
    assert body["next_cursor"] is None

    body_b, status_b = conversion_http.handle_list(
        {"user_id": "userB", "role": "user"}, {})
    assert status_b == 200 and body_b["items"] == []

    body, status = conversion_http.handle_list(ident_a, {"group": "all"})
    assert status == 400 and body["code"] == "invalid_argument"
    body, status = conversion_http.handle_list(ident_a, {"limit": "abc"})
    assert status == 400
    body, status = conversion_http.handle_list(ident_a, {"cursor": "@@@@"})
    assert status == 400

    # 空串 owner（本地免登录归一）同样只看自己
    e = _mk_job("", "h2.kfb")
    body, status = conversion_http.handle_list({"user_id": ""}, {})
    assert status == 200 and _ids(body) == [e["id"]]


def test_http_get_and_retry_error_mapping():
    ident_a = {"user_id": "userA", "role": "user"}
    ident_b = {"user_id": "userB", "role": "user"}
    job = _fail_first()

    body, status = conversion_http.handle_get(ident_a, job["id"])
    assert status == 200
    assert body["conversion_job_id"] == job["id"]
    assert "error_detail_internal" not in body

    body, status = conversion_http.handle_get(ident_b, job["id"])
    assert status == 404 and body["code"] == "conversion_not_found"
    body, status = conversion_http.handle_get(ident_a, "cvj_missing")
    assert status == 404

    # 源不可用 → 409
    body, status = conversion_http.handle_retry(ident_a, job["id"],
                                                source_available=False)
    assert status == 409
    assert body["state"] == "failed"

    # 他人任务 → 404（不泄露存在性）
    body, status = conversion_http.handle_retry(ident_b, job["id"])
    assert status == 404 and body["code"] == "conversion_not_found"

    # 正常重试 → 200，同 id 重新入队
    body, status = conversion_http.handle_retry(ident_a, job["id"],
                                                source_available=True)
    assert status == 200
    assert body["conversion_job_id"] == job["id"]
    assert body["state"] == "queued"

    # 已回 queued 再试 → 409
    body, status = conversion_http.handle_retry(ident_a, job["id"])
    assert status == 409


# --------------------------------------------------------------------------- #
# public_catalog：产品向格式目录
# --------------------------------------------------------------------------- #
def test_public_catalog_rows_and_product_copy():
    catalog = reg.public_catalog()
    assert catalog and isinstance(catalog, list)

    by_ext = {}
    ids = set()
    for item in catalog:
        # 契约字段齐全
        assert set(item) == {"id", "display_name", "extensions", "capability",
                             "canonical_format", "bundle_required",
                             "import_mode", "limits",
                             "selectable_for_upload"}
        assert item["id"] not in ids
        ids.add(item["id"])
        assert item["capability"] in (reg.CAP_NATIVE_SINGLE_FILE,
                                      reg.CAP_NATIVE_BUNDLE,
                                      reg.CAP_CONVERT_REQUIRED)
        assert item["import_mode"] in ("direct", "convert", "bundle")
        assert isinstance(item["limits"], list)
        for ext in item["extensions"]:
            assert ext not in by_ext      # 扩展名跨行不重叠
            by_ext[ext] = item

    # 关键扩展名均在目录且能力/导入模式正确
    assert by_ext[".kfb"]["capability"] == reg.CAP_CONVERT_REQUIRED
    assert by_ext[".kfb"]["import_mode"] == "convert"
    assert by_ext[".kfb"]["canonical_format"] == "bigtiff"
    assert by_ext[".kfbf"]["capability"] == reg.CAP_CONVERT_REQUIRED
    assert by_ext[".kfbf"]["import_mode"] == "convert"
    assert by_ext[".kfbf"]["canonical_format"] == "ome-tiff"
    assert by_ext[".mrxs"]["capability"] == reg.CAP_NATIVE_BUNDLE
    assert by_ext[".mrxs"]["import_mode"] == "bundle"
    assert by_ext[".mrxs"]["bundle_required"] is True

    # OME-TIFF 复合后缀单列一行，明确可见
    assert ".ome.tif" in by_ext and ".ome.tiff" in by_ext
    assert by_ext[".ome.tif"] is by_ext[".ome.tiff"]
    assert by_ext[".ome.tif"]["id"] == "ome-tiff"
    assert by_ext[".ome.tif"]["capability"] == reg.CAP_NATIVE_SINGLE_FILE
    assert by_ext[".ome.tif"]["import_mode"] == "direct"
    assert set(reg.ome_extensions()) == {".ome.tif", ".ome.tiff"}

    # 目录覆盖 _FORMATS 全部扩展名（防两表漂移）
    assert set(reg._FORMATS) <= set(by_ext)  # noqa: SLF001

    # 产品文案：不得残留过时表述；KFB/KFBF 用转换向文案且可选上传
    blob = json.dumps(catalog, ensure_ascii=False)
    assert "尚未接入上传" not in blob
    assert by_ext[".kfb"]["selectable_for_upload"] is True
    assert by_ext[".kfbf"]["selectable_for_upload"] is True
    assert any("BigTIFF" in s for s in by_ext[".kfb"]["limits"])
    assert any("OME-TIFF" in s for s in by_ext[".kfbf"]["limits"])
    assert any("zip" in s for s in by_ext[".mrxs"]["limits"])

    # 返回的是新副本：调用方改写不污染模块状态
    catalog[0]["extensions"].append(".mutated")
    assert ".mutated" not in json.dumps(reg.public_catalog())

    # lookup 引擎判定词表未被目录改动（.ome.tif 仍按末段 .tif 归类）
    assert reg.lookup("a.ome.tif")["ext"] == ".tif"
    assert reg.lookup("a.ome.tif")["capability"] == reg.CAP_NATIVE_SINGLE_FILE
