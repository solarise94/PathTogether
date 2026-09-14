# -*- coding: utf-8 -*-
"""W5 B07/B09/B10：worker 故障注入与崩溃恢复（store 级，真实 PG + fake 传输）。

B07：转存后/下载后/入库后三处崩溃（hooks 注入点在阶段与对账凭证落库
     之后抛错模拟进程死亡），重启 worker 先对账：不二次转存
     （transfer_task_id + poll/list_batch_copies）、不重下载
     （source_sha256 + 暂存文件在盘）、不重复入库（ingest_token）。
B09：部分失败只重试失败项；取消仅释放未消费预占，成功项保持 ready。
B10：清理只针对本批路径；越界路径拒绝；清理失败不回滚 ready；开关关闭
     停止接收新任务、既有任务仍可查。

不需要真实转换器：入库默认钩子只落幂等凭证（真实转换/入库收口由 app
集成层注入 hooks，见 baidu_import_store 模块 docstring 的简化声明）。
"""
import os

import psycopg
import pytest

import baidu_import_http as http
import baidu_import_store as store
import kfb.converter as kfb_converter
from _baidu_helpers import (expire_batch_lease, install_fake,
                            make_ready_enumeration)
from _tiff_fixtures import make_tiff_bytes

OWNER = "u-baidu-rec"
IDENT = {"role": "user", "user_id": OWNER}


class Crash(RuntimeError):
    """模拟进程崩溃（阶段/凭证已落库，后续收口未执行）。"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY",
                       "test-baidu-share-secret-key-2026-09-14")
    up = tmp_path / "uploads"
    up.mkdir()
    monkeypatch.setenv("UPLOAD_DIR", str(up))
    monkeypatch.setattr(kfb_converter, "DEFAULT_MIN_FREE_BYTES", 0)


def _sql(fn):
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            return fn(cur)
    finally:
        conn.close()


def _item_rows(batch_id):
    def q(cur):
        cur.execute(
            "SELECT id, name, stage, error_code, transfer_task_id, "
            "staging_path, source_sha256, ingest_token, attempt, "
            "cleanup_state FROM baidu_import_items WHERE batch_id=%s "
            "ORDER BY name", (batch_id,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    return _sql(q)


def _make_batch(monkeypatch, paths, tmp_path, entries=None, **kw):
    entries = ENTRIES if entries is None else entries
    fake = install_fake(monkeypatch, entries=entries)
    _, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=entries)
    ids = [by_path[p.lstrip("/")]["id"] for p in paths]
    batch = store.create_import(OWNER, enum_id, ids, **kw)
    return fake, batch


def _tiff_entries():
    a = make_tiff_bytes()
    b = make_tiff_bytes(h=48, w=64)
    return [
        {"path": "/a.tif", "size": len(a), "content": a},
        {"path": "/b.tif", "size": len(b), "content": b},
    ]


ENTRIES = None  # 由 fixture 填充真实 TIFF，占位避免导入期生成


@pytest.fixture(autouse=True)
def _tiff_entries_ready():
    global ENTRIES
    ENTRIES = _tiff_entries()


# --------------------------------------------------------------------------- #
# B07：三处崩溃
# --------------------------------------------------------------------------- #

def test_b07_crash_after_transfer_no_double_transfer(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="c1")
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path,
                        hooks={"on_transfer_persisted": lambda item:
                               (_ for _ in ()).throw(Crash("boom"))})
    rows = _item_rows(batch["id"])
    assert rows[0]["stage"] == "transferring"
    assert rows[0]["transfer_task_id"]  # 恢复凭证已落库
    assert fake.counters()["transfer"] == 1
    # 崩溃重启：租约过期后重领 → poll succeeded → 不再第二次转存
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    assert fake.counters()["transfer"] == 1  # 关键断言
    assert view["items"][0]["stage"] == "ready"
    assert _item_rows(batch["id"])[0]["ingest_token"]


def test_b07_crash_after_transfer_reconcile_via_copies(monkeypatch, tmp_path):
    # worker 重启（进程内 task 登记丢失，poll → unknown）：
    # 必须先 list_batch_copies 对账，副本在则不重转存
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="c2")
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path,
                        hooks={"on_transfer_persisted": lambda item:
                               (_ for _ in ()).throw(Crash("boom"))})
    fake.disable_task_registry = True  # 模拟新进程
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    assert fake.counters()["transfer"] == 1  # 对账成功，无二次转存
    # fake 副本对账读数 ≥ 1（list_share_page 与 list_batch_copies 共用
    # list 计数，此处只断言未新增 transfer/download/delete 之外的副作用）
    assert fake.counters()["delete"] == 1  # 成功后清理本批副本


def test_b07_crash_after_download_no_double_download(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="c3")
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path,
                        hooks={"on_downloaded": lambda item:
                               (_ for _ in ()).throw(Crash("boom"))})
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "validating"
    assert row["source_sha256"] and row["staging_path"]
    assert os.path.isfile(row["staging_path"])  # 暂存文件在盘
    assert fake.counters()["download"] == 1
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    assert fake.counters()["download"] == 1  # sha 在盘 → 不重下载
    assert view["items"][0]["stage"] == "ready"


def test_b07_crash_after_ingest_no_duplicate_ingest(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="c4")
    ingest_calls = []

    def counting_hook(item):
        ingest_calls.append(item["id"])
        raise Crash("after ingest")

    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path,
                        hooks={"on_ingested": counting_hook})
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "ready" and row["ingest_token"]  # 凭证已落库
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                           hooks={"on_ingested": counting_hook})
    assert view["state"] == "succeeded"
    # ready + ingest_token 条目被跳过：入库钩子不再触发（无重复入库）
    assert len(ingest_calls) == 1
    assert _item_rows(batch["id"])[0]["ingest_token"] == row["ingest_token"]


# --------------------------------------------------------------------------- #
# B09：部分失败 / 重试 / 取消
# --------------------------------------------------------------------------- #

def test_b09_partial_fail_retry_only_failed(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif", "/b.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="r1")
    fake.fail_download_names = {"b.tif"}
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "partial_failed"
    stages = {i["name"]: i["stage"] for i in view["items"]}
    assert stages == {"a.tif": "ready", "b.tif": "failed"}
    transfers_before = fake.counters()["transfer"]
    # 成功项不重跑：只能选择失败项
    good_id = [i["id"] for i in view["items"] if i["name"] == "a.tif"][0]
    with pytest.raises(store.ConflictError):
        store.retry_items(batch["id"], OWNER, [good_id])
    bad_id = [i["id"] for i in view["items"] if i["name"] == "b.tif"][0]
    retried = store.retry_items(batch["id"], OWNER, [bad_id])
    assert retried["state"] == "queued"
    fake.fail_download_names = set()
    view2 = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view2["state"] == "succeeded"
    assert {i["stage"] for i in view2["items"]} == {"ready"}
    # 重跑只重试失败项：a.tif 不重转存；b.tif 副本仍在（首跑已转存、
    # 仅下载失败）→ 对账后也无需二次转存，直接重下载
    assert fake.counters()["transfer"] == transfers_before
    assert fake.counters()["download"] == 3  # a×1 + b×2


def test_b09_cancel_releases_unconsumed_reservation(monkeypatch, tmp_path):
    released = []
    monkeypatch.setattr(store, "_release_reservation",
                        lambda rid: released.append(rid))

    def hook(user_id, nbytes):
        return "upr_cancel_1"

    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              entries=ENTRIES, quota_hook=hook,
                              idempotency_key="x1")
    store.request_cancel(batch["id"], OWNER)
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "cancelled"
    assert all(i["stage"] == "cancelled" for i in view["items"])
    assert released == ["upr_cancel_1"]  # 未消费预占已释放
    c = fake.counters()
    assert (c["transfer"], c["download"], c["delete"]) == (0, 0, 0)


def test_b09_cancel_after_ready_keeps_products(monkeypatch, tmp_path):
    # 一项 ready 后取消：成功产物保持 ready，预占不重复释放（已消费/
    # 部分消费语义由配额收口持有）
    released = []
    monkeypatch.setattr(store, "_release_reservation",
                        lambda rid: released.append(rid))
    consumed = []
    monkeypatch.setattr(store, "_consume_reservation",
                        lambda rid, n: consumed.append((rid, n)))

    def hook(user_id, nbytes):
        return "upr_cancel_2"

    fake, batch = _make_batch(monkeypatch, ["/a.tif", "/b.tif"], tmp_path,
                              entries=ENTRIES, quota_hook=hook,
                              idempotency_key="x2")
    # 首个条目 ready 后、其余完成前取消（按计数注入，规避条目顺序不确定）
    state = {"ingested": 0}

    def cancel_after_first_ready(item):
        state["ingested"] += 1
        if state["ingested"] == 1:
            store.request_cancel(batch["id"], OWNER)
            raise Crash("cancel mid-run")

    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path,
                        hooks={"on_ingested": cancel_after_first_ready})
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    stages = {i["name"]: i["stage"] for i in view["items"]}
    assert sorted(stages.values()) == ["cancelled", "ready"]
    assert view["state"] == "partial_failed"  # 有 ready 产物，不整批失败
    assert released == []  # 已有 ready 产物 → 不整批释放预占
    assert consumed and consumed[0][0] == "upr_cancel_2"


def test_b09_cancel_terminal_batch_noop(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="x3")
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    again = store.request_cancel(batch["id"], OWNER)
    assert again["state"] == "succeeded"  # 已终结返回当前状态
    assert _item_rows(batch["id"])[0]["stage"] == "ready"  # 产物不删


# --------------------------------------------------------------------------- #
# B10：清理与开关
# --------------------------------------------------------------------------- #

def test_b10_cleanup_only_batch_paths(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="k1")
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    assert fake.deletes and all(
        p.startswith(batch["id"] + "/") for p in fake.deletes)
    assert fake.copies.get(batch["id"], {}) == {}  # 副本已清
    assert view["cleanup_state"] == "succeeded"


def test_b10_cleanup_foreign_path_rejected(monkeypatch, tmp_path):
    from baidu_adapter import AdapterError
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="k2")
    with pytest.raises(AdapterError) as ei:
        fake.cleanup_batch_copies(batch["id"],
                                  ["otherbatch/not-mine.svs"])
    assert ei.value.code == "cleanup_path_rejected"


def test_b10_cleanup_failure_keeps_ready(monkeypatch, tmp_path):
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="k3")
    fake.fail_cleanup = True
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    # 清理失败：批次仍成功、条目仍 ready（不反向破坏）
    assert view["state"] == "succeeded"
    assert view["cleanup_state"] == "failed"
    assert _item_rows(batch["id"])[0]["stage"] == "ready"
    # 清理可重试：关闭故障后重跑收口
    fake.fail_cleanup = False
    store.retry_cleanup(batch["id"])
    assert store.get_import(batch["id"], OWNER)["cleanup_state"] == \
        "succeeded"


def test_b10_flags_disabled_503_existing_rows_visible(monkeypatch, tmp_path):
    # 先用可用适配器建一条已 ready 的枚举
    fake, enum_id, by_path = make_ready_enumeration(
        monkeypatch, owner=OWNER, entries=ENTRIES)
    # 开关关闭（capabilities 不可用）：新建枚举 503
    class DisabledAdapter:
        def capabilities(self):
            return {"enumeration_available": False,
                    "import_available": False,
                    "reason_code": "enumeration_disabled", "limits": {}}

        def counters(self):
            return {"list": 0, "transfer": 0, "download": 0, "delete": 0}

    monkeypatch.setattr(store, "get_adapter", DisabledAdapter)
    body, status = http.create_enumeration(
        IDENT, {"share_text": "https://pan.baidu.com/s/1TestShareId99"})
    assert status == 503 and body["code"] == "enumeration_disabled"
    # 既有任务仍可查（不因开关丢失）
    view = store.get_enumeration(enum_id, OWNER)
    assert view["state"] == "ready"
    # capabilities 透传同样原因
    body2, status2 = http.capabilities(IDENT)
    assert status2 == 200 and body2["reason_code"] == "enumeration_disabled"
    # fake 仍可继续导入既有枚举（已接受任务收口）
    batch = store.create_import(
        OWNER, enum_id, [by_path["a.tif"]["id"]], idempotency_key="k4")
    view2 = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view2["state"] == "succeeded"
