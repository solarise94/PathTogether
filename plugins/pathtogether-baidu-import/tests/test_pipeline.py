# -*- coding: utf-8 -*-
"""C5-B 端到端管线测试（stub 平台 + fake 源 + 真原生转换）。

覆盖（对应合同测试矩阵的插件侧行）：

- 受控替身全链路（T1 插件侧）：fake 源下载 → 原生核心转换（合成 KFB/
  KFBF）→ producer 导入交付 → published 回执；scratch 释放恰一次。
- 重启续跑（T12）：交付中途崩溃 → journal+status 权威 offset 续传，
  无重复 begin；begin 前后两个崩溃窗口的窄化处理。
- 回执丢失 → status 读回（T3 插件侧）。
- 409 offset_conflict 恢复（T5 插件侧）。
- 413 配额 → 零传输（T17 插件侧）。
- 429 Retry-After（T18 插件侧）。
- grant 撤销（begin 前）→ 不 begin（T9 插件侧）。
- 插件停用（401）mid-flight → 停止（T9 插件侧）。
- cleanup_not_verified → 重试至受管根空（T13 插件侧）。
- published + 清理失败 → 不重传（T13 插件侧）。
- declared checksum mismatch → 422 路径（T6 插件侧 / §8 裁决 6）。
"""

import hashlib
import json
import time

import pytest

from worker import states
from worker.batch_driver import BatchDriver
from worker.item_task import ItemTask

pytestmark = pytest.mark.c5b


class Crash(Exception):
    """测试注入：模拟进程崩溃（异常向上传播 = 进程死亡）。"""


def _driver(ctx, hooks=None):
    return BatchDriver(ctx, hooks=hooks)


def _batch_view(sandbox, batch_id="bch_c5b"):
    b = sandbox.state.batches[batch_id]
    return {"batch_id": batch_id, "target_project_id":
            b["target_project_id"], "share_url": b["share_url"],
            "extraction_code": b["extraction_code"]}


def _items(sandbox, batch_id="bch_c5b"):
    return [dict(i) for i in sandbox.state.items.values()
            if i["batch_id"] == batch_id]


def _journal_records(sandbox):
    out = {}
    import pathlib
    jdir = pathlib.Path(sandbox.share_data) / "plugin-work" / "inst_c5b" \
        / "journal"
    for p in sorted(jdir.glob("*.json")):
        rec = json.loads(p.read_text())
        out[rec["import_id"]] = rec
    return out


# --------------------------------------------------------------------------- #
# T1 插件侧：受控替身全链路
# --------------------------------------------------------------------------- #

def test_t1_happy_path_kfb_and_kfbf(sandbox):
    sandbox.enqueue_batch()
    ctx = sandbox.build_ctx()
    summary = _driver(ctx).run_once()
    assert summary and summary.get("batch_id") == "bch_c5b"
    outcomes = {o["item_id"]: o for o in summary["outcomes"]}
    assert len(outcomes) == 2
    for o in outcomes.values():
        assert o["stage"] == states.DONE, o
        assert o["slide_id"] and o["import_id"]
        assert o["cleanup_status"] == "cleaned"
    # 平台侧：published + sha256 + accounted_bytes
    rows = sandbox.import_rows()
    assert len(rows) == 2
    for row in rows.values():
        assert row["state"] == "published"
        assert row["sha256_actual"]
        assert row["receipt"]["accounted_bytes"] == row["declared_size"]
        assert row["scratch_released"] is True
        assert row["plugin_cleanup_status"] == "cleaned"
    # 逐字节核对：staging 数据 == 交付产物 sha（平台自算）
    for row in rows.values():
        data = sandbox.state.staging_file(row["import_id"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256_actual"]
        assert row["declared_size"] == len(data)
    # scratch 释放恰一次 / publish 恰一次
    assert sandbox.state.counters["release_scratch"] == 2
    assert sandbox.state.counters["publish"] == 2
    # 受管根已清空；journal 终态且 write_token 已抹除
    for row in rows.values():
        root = sandbox.state.managed_root(row["import_id"])
        assert not root.exists()
    recs = _journal_records(sandbox)
    assert len(recs) == 2
    for rec in recs.values():
        assert rec["stage"] == states.DONE
        assert "write_token" not in rec
        assert rec["receipt"]["state"] == "published"
    # 桥条目已回写终态（平台白名单 stage：ready）+ slide 绑定经 begin
    for item in sandbox.state.items.values():
        assert item["stage"] == "ready"
        assert item["slide_id"]
    # 源副本清理：ready 项副本已删（fake 记账）
    src = ctx.source
    assert src.counters()["delete"] >= 2
    # KFBF 伴随 channel.json 被传入转换（fake 分享里带伴随条目）
    assert any(d[0].endswith("channel.json")
               for d in src.downloads)


def test_native_delivered_as_is(sandbox):
    """native 单文件（tif）：不转换按原样交付。"""
    content = b"II*\x00" + b"native-bytes" * 500
    src = sandbox.fake_source_with([
        {"path": "/share/plain.tif", "fs_id": "9200001",
         "size": len(content), "content": content}])
    sandbox.state.add_batch(
        "bch_native", share_url="https://pan.baidu.com/s/fakeNative",
        extraction_code=None,
        items=[{"item_id": "itm_native", "name": "plain.tif",
                "fs_id": "9200001", "source_size": len(content)}])
    ctx = sandbox.build_ctx(source=src)
    summary = _driver(ctx).run_once()
    (out,) = summary["outcomes"]
    assert out["stage"] == states.DONE
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "published"
    data = sandbox.state.staging_file(row["import_id"]).read_bytes()
    assert data == content, "native 原样交付（无转换）"
    assert row["declared_size"] == len(content)


# --------------------------------------------------------------------------- #
# T12：重启续跑
# --------------------------------------------------------------------------- #

def test_t12_restart_mid_delivery_resumes_without_duplicate_begin(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    crashes = {"n": 0}

    def after_write(offset):
        crashes["n"] += 1
        if crashes["n"] >= 3:   # 第 3 块确认后「进程死亡」
            raise Crash("mid-delivery restart")

    ctx1 = sandbox.build_ctx(hooks={"after_write": after_write})
    batch, items = _batch_view(sandbox), _items(sandbox)
    with pytest.raises(Crash):
        ItemTask(ctx1, batch, items[0]).run()
    assert sandbox.state.counters["begin"] == 1
    assert sandbox.state.counters["write"] >= 3
    offset_at_crash = sandbox.import_rows()[
        next(iter(sandbox.import_rows()))]["confirmed_offset"]
    assert offset_at_crash > 0

    # —— 重启：全新组件组（journal 从磁盘加载；权威 offset 从 status） ——
    ctx2 = sandbox.build_ctx()
    out = ItemTask(ctx2, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.DONE
    assert out["cleanup_status"] == "cleaned"
    # 无重复 begin；无重复字节（平台自算 sha == 回执 sha、字节齐一）
    assert sandbox.state.counters["begin"] == 1
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "published"
    assert row["sha256_actual"] == row["receipt"]["sha256"]
    assert row["receipt"]["accounted_bytes"] == row["declared_size"]
    # journal 终态 + 凭证抹除
    (rec,) = _journal_records(sandbox).values()
    assert rec["stage"] == states.DONE
    assert "write_token" not in rec


def test_t12_crash_before_begin_restarts_cleanly(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))

    def before_begin():
        raise Crash("pre-begin")

    ctx1 = sandbox.build_ctx(hooks={"before_begin": before_begin})
    with pytest.raises(Crash):
        ItemTask(ctx1, _batch_view(sandbox), _items(sandbox)[0]).run()
    # begin 前崩溃：stub 无 import；journal 只有 pending 记录
    assert sandbox.state.counters.get("begin", 0) == 0
    assert sandbox.import_rows() == {}
    recs = _journal_records(sandbox)
    assert any(i.startswith("pending-") for i in recs)
    # 重启：pending 记录照常 begin → 全链路完成
    ctx2 = sandbox.build_ctx()
    out = ItemTask(ctx2, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.DONE
    assert sandbox.state.counters["begin"] == 1


def test_t12_crash_after_begin_response_documented_window(sandbox):
    """begin 响应已到、journal 未写 token 的窄窗口：重放 begin 拿不回
    token（§1.2 不重发）→ 条目失败 write_token_lost，绝不第二个任务。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))

    def after_begin_response(resp):
        raise Crash("post-begin")

    ctx1 = sandbox.build_ctx(
        hooks={"after_begin_response": after_begin_response})
    with pytest.raises(Crash):
        ItemTask(ctx1, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert sandbox.state.counters["begin"] == 1
    ctx2 = sandbox.build_ctx()
    out = ItemTask(ctx2, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "write_token_lost"
    assert out["cleanup_status"] == "no_token"
    # 重放 begin（同键同载荷）不新建任务
    assert sandbox.state.counters["begin"] == 2
    assert len(sandbox.import_rows()) == 1
    assert sandbox.state.counters.get("write", 0) == 0


# --------------------------------------------------------------------------- #
# T3 插件侧：commit 响应丢失 → status 读回
# --------------------------------------------------------------------------- #

def test_t3_lost_commit_response_receipt_via_status(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    sandbox.state.lose_commit_response = 1
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.DONE
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "published"
    # 回执幂等：publish 恰一次、无第二份资产（commit 重试安全——幂等回执）
    assert sandbox.state.counters["publish"] == 1
    assert sandbox.state.counters["commit"] >= 1
    assert len(sandbox.import_rows()) == 1


def test_t3_commit_replayed_returns_same_receipt(sandbox):
    """commit 重发（客户端重试）→ 同一回执、不再结算。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.DONE
    (iid, row), = sandbox.import_rows().items()
    r1 = ctx.platform.import_commit(iid, row["write_token"],
                                    row["sha256_actual"])
    assert r1["state"] == "published" and r1["revision"] == 1
    r2 = ctx.platform.import_status(iid, row["write_token"])
    assert r2["receipt"]["sha256"] == r1["sha256"]
    assert sandbox.state.counters["publish"] == 1


# --------------------------------------------------------------------------- #
# T5 插件侧：offset_conflict 恢复
# --------------------------------------------------------------------------- #

def test_t5_offset_conflict_recovery(sandbox):
    content = b"II*\x00" + b"offset-test-payload" * 300
    src = sandbox.fake_source_with([
        {"path": "/share/off.tif", "fs_id": "9300001", "size": len(content),
         "content": content}])
    sandbox.state.add_batch(
        "bch_off", share_url="https://pan.baidu.com/s/fakeOff",
        extraction_code=None,
        items=[{"item_id": "itm_off", "name": "off.tif",
                "fs_id": "9300001", "source_size": len(content)}])
    drifted = {"done": False}

    def write_hook(row, offset):
        if drifted["done"] or offset != 0:
            return None
        drifted["done"] = True
        # 模拟断线恢复漂移：前一会话已权威确认 100 字节（本会话不知）
        staging = sandbox.state.staging_file(row["import_id"])
        with open(staging, "ab") as fh:
            fh.write(content[:100])
        row["confirmed_offset"] = 100
        return None

    sandbox.state.write_hook = write_hook
    ctx = sandbox.build_ctx(source=src)
    out = ItemTask(ctx, _batch_view(sandbox, "bch_off"),
                   _items(sandbox, "bch_off")[0]).run()
    assert out["stage"] == states.DONE
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "published"
    data = sandbox.state.staging_file(row["import_id"]).read_bytes()
    assert data == content, "续传无空洞无重复字节"
    assert sandbox.state.counters["write"] >= 2


# --------------------------------------------------------------------------- #
# T17 插件侧：413 配额 → 停止，零传输
# --------------------------------------------------------------------------- #

def test_t17_quota_refusal_on_scratch_stops_before_transfer(sandbox):
    """转换前 scratch 补占被拒 → 不发任何 write（§4.2 不赌）。"""
    sandbox.enqueue_batch(names=("sample-fl.kfbf",))
    src_size = len(sandbox.fixtures["kfbf_bytes"])
    # 允许 begin（declared=1+scratch=src），拒绝转换期 topup（2×src）
    sandbox.state.quota_bytes = src_size + 2
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "upload_quota_exceeded"
    assert sandbox.state.counters.get("write", 0) == 0, "配额拒绝后零传输"
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "cancelled"
    assert row["plugin_cleanup_status"] == "cleaned"
    assert row["scratch_released"] is True


def test_t17_quota_refusal_on_final_topup_stops_before_transfer(sandbox):
    sandbox.enqueue_batch(names=("sample-fl.kfbf",))
    src_size = len(sandbox.fixtures["kfbf_bytes"])
    # 允许 scratch 峰值（1 + 2×src），拒绝任何 final topup（只增 ≥1）
    sandbox.state.quota_bytes = 2 * src_size + 1
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "upload_quota_exceeded"
    assert sandbox.state.counters.get("write", 0) == 0
    assert sandbox.state.counters["topup"] == 1


# --------------------------------------------------------------------------- #
# T18 插件侧：429 Retry-After（真 HTTP）
# --------------------------------------------------------------------------- #

def test_t18_rate_limited_write_waits_and_succeeds(sandbox):
    content = b"II*\x00" + b"rate-limit-payload" * 3000
    src = sandbox.fake_source_with([
        {"path": "/share/rl.tif", "fs_id": "9400001", "size": len(content),
         "content": content}])
    sandbox.state.add_batch(
        "bch_rl", share_url="https://pan.baidu.com/s/fakeRL",
        extraction_code=None,
        items=[{"item_id": "itm_rl", "name": "rl.tif", "fs_id": "9400001",
                "source_size": len(content)}])
    # 只限 /write（suffix 匹配）：第 1 次后窗口内 429，Retry-After=1s
    sandbox.state.rate_limits["/write"] = {"n": 1, "window": 0.9,
                                           "retry_after": 1,
                                           "suffix": True}
    ctx = sandbox.build_ctx(source=src)
    t0 = time.monotonic()
    out = ItemTask(ctx, _batch_view(sandbox, "bch_rl"),
                   _items(sandbox, "bch_rl")[0]).run()
    elapsed = time.monotonic() - t0
    assert out["stage"] == states.DONE
    assert sandbox.state.counters.get("rate_limited", 0) >= 1
    assert elapsed >= 0.9, "应按 Retry-After 等待"


# --------------------------------------------------------------------------- #
# T9 插件侧：grant 撤销 / 插件停用
# --------------------------------------------------------------------------- #

def test_t9_grant_revoked_before_begin_no_begin(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    sandbox.state.revoke_grant("pig_c5b_main")
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "import_grant_invalid:grant_revoked"
    assert sandbox.state.counters.get("begin", 0) == 1  # 被 403 拒的那一次
    assert sandbox.state.counters.get("write", 0) == 0
    assert sandbox.import_rows() == {}, "未建任务（grant 无效即拒）"


def test_t9_grant_missing_no_begin_at_all(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    ctx = sandbox.build_ctx(grants_seed="")   # 未登记任何 grant
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "grant_missing"
    assert sandbox.state.counters.get("begin", 0) == 0


def test_t9_plugin_disabled_midflight_stops(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    flipped = {"done": False}

    def write_hook(row, offset):
        if not flipped["done"]:
            flipped["done"] = True
            sandbox.state.enabled = False   # 模拟 admin disable
        return None

    sandbox.state.write_hook = write_hook
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == "stopped"
    assert out["error_code"] == "plugin_disabled"
    writes_at_stop = sandbox.state.counters["write"]
    assert writes_at_stop >= 1
    # journal 保持非终态（可再续跑）
    recs = _journal_records(sandbox)
    (rec,) = [r for r in recs.values() if not r.get("terminal")]
    assert rec["write_token"], "停用停止时凭证仍在（续跑需要）"
    # 再次运行：token 交换也 401 → 仍停止、不崩溃、无新副作用
    sandbox.state.enabled = True
    out2 = ItemTask(sandbox.build_ctx(), _batch_view(sandbox),
                    _items(sandbox)[0]).run()
    assert out2["stage"] == states.DONE
    assert sandbox.state.counters["begin"] == 1


# --------------------------------------------------------------------------- #
# T13 插件侧：清理核验
# --------------------------------------------------------------------------- #

def test_t13_cleanup_not_verified_retries_until_empty(sandbox):
    """受管根非空 → 409 cleanup_not_verified + 残余字节 → 退避重删重确认。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    plants = {"n": 0}

    def confirm_hook(row):
        # 前 2 次确认时受管根再现残件（写者残迹竞态 → 平台核验非空）
        if plants["n"] < 2:
            plants["n"] += 1
            root = sandbox.state.managed_root(row["import_id"])
            root.mkdir(parents=True, exist_ok=True)
            (root / "straggler.bin").write_bytes(b"leftover-bytes")

    sandbox.state.cleanup_confirm_hook = confirm_hook
    ctx = sandbox.build_ctx()
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.DONE
    assert out["cleanup_status"] == "cleaned"
    assert plants["n"] == 2
    assert sandbox.state.counters.get("cleanup_not_verified", 0) == 2
    assert sandbox.state.counters["cleanup_confirm"] >= 3
    (row,) = sandbox.import_rows().values()
    assert row["scratch_released"] is True


def test_t13_published_cleanup_failure_keeps_result_no_reupload(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    from worker import cleanup as cleanup_mod
    real_remove = cleanup_mod.remove_tree

    def failing_remove(root):
        real_remove(root)
        root.mkdir(parents=True, exist_ok=True)
        (root / "stuck.bin").write_bytes(b"undeletable")   # 永久残件

    cleanup_mod.remove_tree = failing_remove
    try:
        ctx = sandbox.build_ctx()
        out = ItemTask(ctx, _batch_view(sandbox),
                       _items(sandbox)[0]).run()
    finally:
        cleanup_mod.remove_tree = real_remove
    # published 保留；cleanup_failed；绝不重传（不再次 begin/write/commit）
    assert out["stage"] == states.DONE
    assert out["cleanup_status"] == "cleanup_failed"
    assert out["slide_id"]
    (row,) = sandbox.import_rows().values()
    assert row["state"] == "published"
    assert row["plugin_cleanup_status"] in ("none", "pending")
    assert sandbox.state.counters["begin"] == 1
    assert sandbox.state.counters["commit"] == 1
    assert sandbox.state.counters["publish"] == 1
    assert sandbox.state.counters.get("release_scratch", 0) == 0, \
        "清理未确认 → scratch 不释放（§4.2）"
    # write_token 保留（清理重试需要任务级凭证）；receipt 在
    recs = _journal_records(sandbox)
    (rec,) = recs.values()
    assert rec["cleanup_status"] == "cleanup_failed"
    assert rec.get("write_token"), "cleanup_failed 保留凭证供清理重试"
    assert rec["receipt"]["state"] == "published"


def test_cleanup_confirm_idempotent_repeat(sandbox):
    ctx = sandbox.build_ctx()
    content = b"II*\x00abcd"
    r = ctx.platform.import_begin(
        grant_id="pig_c5b_main", project_id="prj_test",
        filename="x.tif", format_ext="tif", declared_size=len(content),
        scratch_bytes=100, idempotency_key="k_cc")
    iid, tok = r["import_id"], r["write_token"]
    ctx.platform.import_write_chunk(iid, tok, 0, content)
    ctx.platform.import_commit(
        iid, tok, hashlib.sha256(content).hexdigest())
    a = ctx.platform.import_cleanup_confirm(iid, tok)
    b = ctx.platform.import_cleanup_confirm(iid, tok)
    assert a["cleanup_status"] == b["cleanup_status"] == "cleaned"
    assert sandbox.state.counters["release_scratch"] == 1, "释放恰一次"


# --------------------------------------------------------------------------- #
# T6 插件侧 / §8 裁决 6：declared checksum mismatch
# --------------------------------------------------------------------------- #

def test_t6_declared_checksum_mismatch_fails_closed(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb",))

    def on_stage(stage):
        # 进入 delivering 时篡改本地产物：已记录的 declared sha256 与
        # 实际交付字节背离 → 平台自算 != 声明
        if stage != states.DELIVERING:
            return
        import pathlib
        for rec in _journal_records(sandbox).values():
            if rec.get("artifact_rel") and not rec.get("terminal"):
                root = pathlib.Path(sandbox.share_data) / "plugin-work" / \
                    "inst_c5b" / "imports" / rec["import_id"]
                art = root / rec["artifact_rel"]
                if art.is_file():
                    with open(art, "ab") as fh:
                        fh.write(b"mutated")

    ctx = sandbox.build_ctx(hooks={"on_stage": on_stage})
    out = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert out["stage"] == states.FAILED
    assert out["error_code"] == "declared_checksum_mismatch"
    assert sandbox.state.counters.get("declared_checksum_mismatch", 0) == 1
    (row,) = sandbox.import_rows().values()
    # §8 裁决 6：无 intent；任务被插件取消收口（不重传）
    assert row["state"] == "cancelled"
    assert row["plugin_cleanup_status"] == "cleaned"
    assert sandbox.state.counters.get("publish", 0) == 0


# --------------------------------------------------------------------------- #
# 驱动层：取消 / 租约
# --------------------------------------------------------------------------- #

def test_driver_cancel_requested_cancels_items_without_begin(sandbox):
    sandbox.enqueue_batch()
    sandbox.state.batches["bch_c5b"]["cancel_requested"] = True
    ctx = sandbox.build_ctx()
    summary = _driver(ctx).run_once()
    for item in sandbox.state.items.values():
        assert item["stage"] == "cancelled"
    assert sandbox.state.counters.get("begin", 0) == 0
    assert summary["outcomes"] == []


def test_driver_lease_lost_abandons_quietly(sandbox):
    sandbox.enqueue_batch(names=("sample-bf.kfb", "sample-fl.kfbf"))
    stolen = {"done": False}

    def on_item_outcome(outcome):
        # 第一个条目收口后，租约被其他 worker 重领（fence 失效）
        if not stolen["done"]:
            stolen["done"] = True
            sandbox.state.batches["bch_c5b"]["lease_token"] = "lst_other"

    ctx = sandbox.build_ctx()
    summary = _driver(ctx, hooks={"on_item_outcome": on_item_outcome}).run_once()
    assert summary.get("abandoned") == "lease_lost"
    # 第一条目已完成发布；第二条未推进（新 owner 接管）
    assert sandbox.state.counters["begin"] == 1
    assert sandbox.state.counters["publish"] == 1


def test_driver_no_claim_returns_none(sandbox):
    ctx = sandbox.build_ctx()
    assert _driver(ctx).run_once() is None


def test_driver_reports_progress_stages(sandbox):
    """条目阶段推进经桥回写（阶段 + 终态引用）。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    stages_seen = []
    ctx = sandbox.build_ctx(
        hooks={"on_stage": lambda s: stages_seen.append(s)})
    summary = _driver(ctx).run_once()
    assert summary["outcomes"][0]["stage"] == states.DONE
    for s in ("downloading", "transforming", "validating", "delivering"):
        assert s in stages_seen
    item = sandbox.state.items["itm_bch_c5b_0"]
    assert item["stage"] == "ready"
    assert item["slide_id"]


# --------------------------------------------------------------------------- #
# 验收补充：取消回写、心跳临时故障、租约丢失、终态重放
# --------------------------------------------------------------------------- #

_FAST_HB = {"PT_BAIDU_HEARTBEAT_INTERVAL": "0.1"}


def test_driver_cancel_mid_batch_keeps_finished_item_ready(sandbox):
    """批次取消在第一条目收口后到达：第一条目保持 ready，只把未开始的
    条目报告为 cancelled。"""
    sandbox.enqueue_batch()

    def on_item_outcome(outcome):
        if outcome["stage"] == states.DONE:
            sandbox.state.batches["bch_c5b"]["cancel_requested"] = True
            time.sleep(0.5)  # 让心跳带回取消信号

    ctx = sandbox.build_ctx(env=_FAST_HB)
    summary = _driver(ctx, hooks={"on_item_outcome": on_item_outcome}).run_once()
    assert [o["stage"] for o in summary["outcomes"]] == [states.DONE]
    stages = sorted(i["stage"] for i in sandbox.state.items.values())
    assert stages == ["cancelled", "ready"]
    assert sandbox.state.counters["begin"] == 1


def test_driver_transient_heartbeat_error_does_not_cancel(sandbox):
    """心跳网络抖动（未超过租约时长）不得被当成租约丢失去取消在途导入。"""
    from worker import errors
    sandbox.enqueue_batch()
    ctx = sandbox.build_ctx(env=_FAST_HB)
    real = ctx.platform.baidu_heartbeat
    calls = {"n": 0}

    def flaky(batch_id, lease_token):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise errors.TransportError("connection reset")
        return real(batch_id, lease_token)

    ctx.platform.baidu_heartbeat = flaky
    summary = _driver(ctx, hooks={
        "on_item_outcome": lambda o: time.sleep(0.5)}).run_once()
    assert calls["n"] > 3, "心跳应在失败后继续重试"
    assert [o["stage"] for o in summary["outcomes"]] == [states.DONE] * 2
    assert sandbox.state.counters.get("cancel", 0) == 0
    for item in sandbox.state.items.values():
        assert item["stage"] == "ready"


def test_driver_lease_lost_mid_item_stops_without_touching_import(sandbox):
    """条目进行中租约被夺：停在可续跑点——不取消平台导入、不清受管根、
    不回写条目（新 owner 接管）。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))

    def on_stage(stage):
        if stage == states.TRANSFORMING:
            sandbox.state.batches["bch_c5b"]["lease_token"] = "lst_other"
            time.sleep(0.5)  # 心跳发现租约被夺

    ctx = sandbox.build_ctx(env=_FAST_HB, hooks={"on_stage": on_stage})
    summary = _driver(ctx).run_once()
    assert summary.get("abandoned") == "lease_lost"
    assert sandbox.state.counters.get("cancel", 0) == 0
    rows = sandbox.import_rows()
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["state"] in ("created", "writing")
    assert sandbox.state.managed_root(row["import_id"]).exists()
    rec = next(iter(_journal_records(sandbox).values()))
    assert rec["stage"] not in states.TERMINAL
    assert rec.get("write_token"), "续跑需要任务级凭证"
    assert sandbox.state.items["itm_bch_c5b_0"]["stage"] == "queued"


def test_rerun_of_locally_finished_item_replays_result(sandbox):
    """本地已收口但平台没收到回写（报告时断网）：再次推进只重放结果，
    不因 write_token 已抹除改判失败、不重新 begin。"""
    sandbox.enqueue_batch(names=("sample-bf.kfb",))
    ctx = sandbox.build_ctx()
    first = _driver(ctx).run_once()["outcomes"][0]
    assert first["stage"] == states.DONE
    begins = sandbox.state.counters["begin"]
    item = dict(sandbox.state.items["itm_bch_c5b_0"], item_id="itm_bch_c5b_0",
                stage="ingesting")
    again = ItemTask(sandbox.build_ctx(), _batch_view(sandbox), item).run()
    assert again["stage"] == states.DONE
    assert again["slide_id"] == first["slide_id"]
    assert again["cleanup_status"] == "cleaned"
    assert sandbox.state.counters["begin"] == begins
    rec = next(iter(_journal_records(sandbox).values()))
    assert rec["stage"] == states.DONE


def test_retry_after_local_failure_starts_fresh_attempt(sandbox):
    """平台 retry_items 把失败项重排为 queued（同 item_id）：插件以新幂等键
    开始新一次尝试，而不是重放已终态任务（后者拿不回 write_token）。"""
    sandbox.enqueue_batch(names=("sample-fl.kfbf",))
    src_size = len(sandbox.fixtures["kfbf_bytes"])
    sandbox.state.quota_bytes = src_size + 2
    ctx = sandbox.build_ctx()
    first = ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    assert first["stage"] == states.FAILED
    assert first["error_code"] == "upload_quota_exceeded"

    sandbox.state.quota_bytes = None
    item = dict(_items(sandbox)[0], stage="queued", error_code=None)
    again = ItemTask(sandbox.build_ctx(), _batch_view(sandbox), item).run()
    assert again["stage"] == states.DONE, again
    assert again["import_id"] != first["import_id"]
    rows = sandbox.import_rows()
    assert rows[first["import_id"]]["state"] == "cancelled"
    assert rows[again["import_id"]]["state"] == "published"
    keys = {r["idempotency_key"] for r in rows.values()}
    assert len(keys) == 2
    assert any(k.endswith("-r1") for k in keys)
    recs = _journal_records(sandbox)
    assert recs[again["import_id"]]["stage"] == states.DONE
    assert recs[first["import_id"]]["stage"] == states.FAILED


def test_rerun_after_crash_in_failure_cleanup_finishes_it_first(sandbox):
    """失败收口途中崩溃（terminal 已落、清理未确认）：重跑先把旧尝试的取消与
    受管根清理做完，再开始新尝试。"""
    sandbox.enqueue_batch(names=("sample-fl.kfbf",))
    src_size = len(sandbox.fixtures["kfbf_bytes"])
    sandbox.state.quota_bytes = src_size + 2

    def crash_on_cancel(*_a, **_k):
        raise Crash()

    ctx = sandbox.build_ctx()
    real_cancel = ctx.platform.import_cancel
    ctx.platform.import_cancel = crash_on_cancel
    with pytest.raises(Crash):
        ItemTask(ctx, _batch_view(sandbox), _items(sandbox)[0]).run()
    ctx.platform.import_cancel = real_cancel
    (old_id,) = sandbox.import_rows().keys()
    assert sandbox.import_rows()[old_id]["state"] != "cancelled"

    sandbox.state.quota_bytes = None
    item = dict(_items(sandbox)[0], stage="queued", error_code=None)
    again = ItemTask(sandbox.build_ctx(), _batch_view(sandbox), item).run()
    assert again["stage"] == states.DONE, again
    rows = sandbox.import_rows()
    assert rows[old_id]["state"] == "cancelled"
    assert rows[old_id]["plugin_cleanup_status"] == "cleaned"
    assert not sandbox.state.managed_root(old_id).exists()
    assert _journal_records(sandbox)[old_id]["stage"] == states.FAILED
