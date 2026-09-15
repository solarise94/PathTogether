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
from pathlib import Path

import psycopg
import pytest

import baidu_import_http as http
import baidu_import_store as store
import kfb.converter as kfb_converter
import kfb.converter_fl as kfbf_converter
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
    monkeypatch.setattr(kfbf_converter, "DEFAULT_MIN_FREE_BYTES", 0)


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
            "cleanup_state, conversion_job_id, slide_name, "
            "project_associate_state FROM baidu_import_items "
            "WHERE batch_id=%s ORDER BY name", (batch_id,))
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


# --------------------------------------------------------------------------- #
# 回归：worker 双重领取 / 入库-凭证间隙崩溃 / 运行中取消
# --------------------------------------------------------------------------- #

def _load_worker_module():
    """importlib 加载 scripts/baidu_import_worker.py（scripts 非包）。"""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts", "baidu_import_worker.py")
    spec = importlib.util.spec_from_file_location(
        "baidu_import_worker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _slides_count():
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM slides")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _upload_names():
    return sorted(p.name for p in Path(os.environ["UPLOAD_DIR"]).iterdir())


def test_worker_drain_once_reaches_terminal_state(monkeypatch, tmp_path):
    # P1：claim_batch 已置 running 并持有新鲜租约；若随后 run_batch 按
    # id 二次领取（只接受 queued/租约过期）→ None，批次永远卡 running。
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="w1")
    import baidu_adapter
    monkeypatch.setattr(baidu_adapter, "get_adapter", lambda: fake)
    monkeypatch.setattr(store, "STAGING_ROOT", str(tmp_path / "staging"))
    worker = _load_worker_module()
    n_enum, n_batch = worker.drain_once(worker_id="w-worker")
    assert (n_enum, n_batch) == (0, 1)
    view = store.get_import(batch["id"], OWNER)
    assert view["state"] == "succeeded"  # 一轮 drain 即达终态
    assert [i["stage"] for i in view["items"]] == ["ready"]
    c = fake.counters()
    assert (c["transfer"], c["download"]) == (1, 1)  # 各只一次


def test_crash_between_ingest_and_token_recovers_native(
        monkeypatch, tmp_path):
    # P1：ingest_staging 完成后、ingest_token 落库前崩溃 → 条目停在
    # ingesting 且无凭证；恢复时按标识对账直接收口，不重跑入库
    # （重跑会 name_unavailable，把已成功任务打成 failed）。
    import baidu_ingest
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="w3")
    real_ingest = baidu_ingest.ingest_staging

    def crash_after_ingest(**kw):
        real_ingest(**kw)  # 产物落盘 + 归属登记已完成
        raise Crash("token 未落库即进程死亡")

    monkeypatch.setattr(baidu_ingest, "ingest_staging", crash_after_ingest)
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path)
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "ingesting" and not row["ingest_token"]
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "ready"
    assert row["ingest_token"] == "slide:a.tif"
    # 无重复产物：uploads 单文件、slides 元数据单条、外部调用不重放
    assert _upload_names() == ["a.tif"]
    assert _slides_count() == 1
    c = fake.counters()
    assert (c["transfer"], c["download"]) == (1, 1)


def test_crash_between_ingest_and_token_recovers_convert_kfbf(
        monkeypatch, tmp_path):
    # P1 convert 路径：conversion job 已 ready 但 token 未落库 → 恢复时
    # 按 canonical 名对账（owner/sha/state）直接按成功路径落库。
    import baidu_ingest
    from kfb.fixture_fl import build_synthetic_kfbf
    payload = build_synthetic_kfbf(tmp_path / "src.kfbf").read_bytes()
    entries = [{"path": "/fl.kfbf", "size": len(payload),
                "content": payload}]
    fake, batch = _make_batch(monkeypatch, ["/fl.kfbf"], tmp_path,
                              entries=entries, idempotency_key="w3f")
    real_ingest = baidu_ingest.ingest_staging

    def crash_after_ingest(**kw):
        real_ingest(**kw)
        raise Crash("token 未落库即进程死亡")

    monkeypatch.setattr(baidu_ingest, "ingest_staging", crash_after_ingest)
    with pytest.raises(Crash):
        store.run_batch(batch["id"], fake, staging_root=tmp_path)
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "converting" and not row["ingest_token"]
    expire_batch_lease(batch["id"])
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    assert view["state"] == "succeeded"
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "ready"
    assert row["ingest_token"] and row["ingest_token"].startswith("cvj:")
    assert row["slide_name"] == "fl.ome.tif"
    # 无重复产物：canonical 产物仅一个（.manifest.json/.associated 是
    # 转换 sidecar，不算重复），conversion job 仅一条
    names = _upload_names()
    assert "fl.kfbf" in names and "fl.ome.tif" in names
    assert [n for n in names if n.endswith(".ome.tif")] == ["fl.ome.tif"]
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM conversion_jobs")
            assert int(cur.fetchone()[0]) == 1
    finally:
        conn.close()


def test_running_cancel_stops_remaining_items(monkeypatch, tmp_path):
    # P2：循环内不重读 cancel_requested → 第一项 ready 后的取消请求
    # 拦不住第二项的转存/下载/入库。
    fake, batch = _make_batch(monkeypatch, ["/a.tif", "/b.tif"], tmp_path,
                              entries=ENTRIES, idempotency_key="w4")

    def cancel_after_first(item):
        store.request_cancel(batch["id"], OWNER)

    view = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                           hooks={"on_ingested": cancel_after_first})
    assert view["state"] == "partial_failed"  # 有 ready 产物，不整批失败
    stages = sorted(i["stage"] for i in view["items"])
    assert stages == ["cancelled", "ready"]  # 第二项被取消，不再推进
    c = fake.counters()
    assert (c["transfer"], c["download"]) == (1, 1)  # 第二项未转存/下载
    ready_name = [i["name"] for i in view["items"]
                  if i["stage"] == "ready"][0]
    assert _upload_names() == [ready_name]  # 第二项未入库


def test_reconcile_never_claims_same_name_different_content(
        monkeypatch, tmp_path):
    # 对账只认内容一致的同名产物：owner 既有同名（内容不同）上传时，
    # 对账不得把它认领为本批 ready（首轮入库前也会先过对账），
    # 必须照旧 name_unavailable → failed，既有文件内容原样保留。
    import share_store
    victim = make_tiff_bytes(h=32, w=48)  # 与 ENTRIES 的 a.tif 内容不同
    (Path(os.environ["UPLOAD_DIR"]) / "a.tif").write_bytes(victim)
    share_store.set_slide_meta("a.tif", owner_user_id=OWNER,
                               requester_role="user")
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="w5")
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path)
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "failed"
    assert row["error_code"] == "name_unavailable"
    assert not row["ingest_token"]
    assert (Path(os.environ["UPLOAD_DIR"]) / "a.tif").read_bytes() == victim
    assert _upload_names() == ["a.tif"]
    assert _slides_count() == 1


# --------------------------------------------------------------------------- #
# P1 回归：批次租约 fencing（旧 owner 写回/收口被拒）+ 心跳续期
# --------------------------------------------------------------------------- #

def test_heartbeat_batch_requires_matching_token(monkeypatch, tmp_path):
    # heartbeat_batch 对齐 heartbeat_enumeration，但额外匹配 lease_token：
    # 只有仍持有本次 claim 租约的 (worker, token) 能续期；批次被重领后
    # 旧 token 心跳恒 False（调用方据此安静放弃）。
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="f0")
    claim = store.claim_batch(worker_id="w-a")
    assert claim is not None and claim["batch"]["id"] == batch["id"]
    token = claim["batch"]["lease_token"]
    assert store.heartbeat_batch(batch["id"], "w-a", token) is True
    assert store.heartbeat_batch(batch["id"], "w-a", "not-the-token") is False
    assert store.heartbeat_batch(batch["id"], "w-other", token) is False
    # 租约过期 → 他人重领（token 更新）→ 旧 token 心跳被拒、新 token 可续
    expire_batch_lease(batch["id"])
    claim2 = store.claim_batch(worker_id="w-b")
    token2 = claim2["batch"]["lease_token"]
    assert token2 != token
    assert store.heartbeat_batch(batch["id"], "w-a", token) is False
    assert store.heartbeat_batch(batch["id"], "w-b", token2) is True


def test_stale_owner_writeback_and_finalize_fenced(monkeypatch, tmp_path):
    # 旧 worker 在 on_downloaded 钩子内被新 worker 重领（token 更新），
    # 心跳间隔大于租约（等效无心跳）：旧 worker 后续条目写回被 fence
    # 拒绝 → 安静放弃：不推进条目、不 finalize、不清理副本、绝不覆盖
    # 新 owner 的状态；新 owner 续跑凭对账凭证正常收口。
    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="f1")
    steal = {}

    def steal_lease(item):
        expire_batch_lease(batch["id"])
        claim2 = store.claim_batch(worker_id="w-new")
        assert claim2 is not None and claim2["batch"]["id"] == batch["id"]
        steal["owner"] = claim2["batch"]["lease_owner"]
        steal["token"] = claim2["batch"]["lease_token"]

    view = store.run_batch(
        batch["id"], fake, staging_root=tmp_path,
        lease_seconds=2, heartbeat_interval=3600,
        hooks={"on_downloaded": steal_lease})
    assert steal["owner"] == "w-new" and steal["token"]
    # 旧 run 安静退出：返回当前视图——批次仍 running（未收口到终态）
    assert view["state"] == "running"
    assert view["items"][0]["stage"] == "validating"
    # 旧 owner 未收口：无副本清理（delete=0）；条目未被推进到入库后状态
    assert fake.counters()["delete"] == 0
    assert sorted(fake.copies.get(batch["id"], {})) == ["a.tif"]
    row = _item_rows(batch["id"])[0]
    assert row["stage"] == "validating"
    assert not row["ingest_token"]
    # 丢租约前的合法写入（下载对账凭证）保留 → 新 owner 不重下载
    assert row["source_sha256"] and row["staging_path"]
    # 新 owner 续跑：正常收口；下载/清理各只发生一次
    expire_batch_lease(batch["id"])
    view2 = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                            worker_id="w-new2")
    assert view2["state"] == "succeeded"
    assert fake.counters()["download"] == 1
    assert fake.counters()["delete"] == 1


def test_heartbeat_keeps_lease_during_slow_download(monkeypatch, tmp_path):
    # 小租约（2s）+ 快心跳（0.5s）+ 慢下载（3s）：租约靠续租始终未过期
    # ——原租约到点后（2.5s 时）另一 worker 领不走，批次单 owner 完成。
    import threading
    import time

    fake, batch = _make_batch(monkeypatch, ["/a.tif"], tmp_path,
                              idempotency_key="f2")
    real_download = fake.download_to

    def slow_download(remote_path, dest_path):
        time.sleep(3.0)  # 下载（3s）远超租约（2s）
        return real_download(remote_path, dest_path)

    monkeypatch.setattr(fake, "download_to", slow_download)
    steal = {"got_batch": None}

    def try_steal():
        time.sleep(2.5)  # 原租约已到点、下载未完 → 只能靠心跳续租保住
        c = store.claim_batch(worker_id="w-thief")
        steal["got_batch"] = bool(c and c["batch"]["id"] == batch["id"])

    thief = threading.Thread(target=try_steal, daemon=True)
    thief.start()
    view = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                           lease_seconds=2, heartbeat_interval=0.5)
    thief.join(timeout=10)
    assert steal["got_batch"] is False  # 心跳续租 → 抢不走
    assert view["state"] == "succeeded"
    assert fake.counters()["download"] == 1


def test_long_download_survives_short_lease_with_heartbeat(
        monkeypatch, tmp_path):
    # 长下载不丢批：多条目、每条下载都慢于租约，另一线程周期性抢领；
    # 心跳开着 → 租约始终在本 worker 手里，单 worker 正常跑到 succeeded。
    import threading
    import time

    fake, batch = _make_batch(monkeypatch, ["/a.tif", "/b.tif"], tmp_path,
                              idempotency_key="f3")
    real_download = fake.download_to

    def slow_download(remote_path, dest_path):
        time.sleep(1.2)  # 单条下载即超过租约（1s）
        return real_download(remote_path, dest_path)

    monkeypatch.setattr(fake, "download_to", slow_download)
    steal = {"got_batch": 0}
    done = threading.Event()

    def _lease_owner():
        def q(cur):
            cur.execute(
                "SELECT lease_owner FROM baidu_import_batches WHERE id=%s",
                (batch["id"],))
            row = cur.fetchone()
            return row[0] if row else None
        return _sql(q)

    def try_steal_loop():
        # 先等主 worker 领走（w-solo 持租约）：本用例只考察运行中的
        # 租约保持，不与初始领取竞争（那是一次正常的单赢家竞争）
        while not done.is_set() and _lease_owner() != "w-solo":
            done.wait(0.05)
        while not done.is_set():
            c = store.claim_batch(worker_id="w-thief")
            if c is not None and c["batch"]["id"] == batch["id"]:
                steal["got_batch"] += 1
            done.wait(0.3)

    thief = threading.Thread(target=try_steal_loop, daemon=True)
    thief.start()
    try:
        view = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                               lease_seconds=1, heartbeat_interval=0.3,
                               worker_id="w-solo")
    finally:
        done.set()
        thief.join(timeout=10)
    assert steal["got_batch"] == 0  # 全程无人抢走
    assert view["state"] == "succeeded"
    assert sorted(i["stage"] for i in view["items"]) == ["ready", "ready"]
    assert fake.counters()["download"] == 2


def test_stale_owner_cancel_fenced(monkeypatch, tmp_path):
    # 旧 worker 丢租约后触发取消收口：批次 terminal 落库被 fence 拒绝
    # → 不取消条目、不释放预占（新 owner 会看到 cancel_requested 并
    # 自行按取消语义收口）。
    released = []
    monkeypatch.setattr(store, "_release_reservation",
                        lambda rid: released.append(rid))

    def hook(user_id, nbytes):
        return "upr_fence_cancel"

    fake, batch = _make_batch(monkeypatch, ["/a.tif", "/b.tif"], tmp_path,
                              entries=ENTRIES, quota_hook=hook,
                              idempotency_key="f4")
    steal = {}

    def steal_and_cancel(item):
        expire_batch_lease(batch["id"])
        claim2 = store.claim_batch(worker_id="w-new")
        assert claim2 is not None and claim2["batch"]["id"] == batch["id"]
        steal["token"] = claim2["batch"]["lease_token"]
        store.request_cancel(batch["id"], OWNER)

    view = store.run_batch(
        batch["id"], fake, staging_root=tmp_path,
        lease_seconds=2, heartbeat_interval=3600,
        hooks={"on_transfer_persisted": steal_and_cancel})
    assert steal["token"]
    # 旧 owner 的取消收口被 fence 拒绝：预占未释放、条目未被取消、
    # 批次仍 running；且旧 owner 在丢租约后未再推进条目（a 停在
    # transferring，未下载）
    assert released == []
    assert view["state"] == "running"
    assert sorted(i["stage"] for i in view["items"]) == \
        ["queued", "transferring"]
    assert fake.counters()["download"] == 0
    # 新 owner 续跑：真正按取消语义收口（无 ready 产物 → 释放预占）
    expire_batch_lease(batch["id"])
    view2 = store.run_batch(batch["id"], fake, staging_root=tmp_path,
                            worker_id="w-new2")
    assert view2["state"] == "cancelled"
    assert sorted(i["stage"] for i in view2["items"]) == \
        ["cancelled", "cancelled"]
    assert released == ["upr_fence_cancel"]
