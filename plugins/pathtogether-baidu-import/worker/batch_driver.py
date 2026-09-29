# -*- coding: utf-8 -*-
"""批次驱动循环（C5-B；合同 §6.3 插件作为 claimer）。

镜像平台 ``run_claimed_batch`` 的租约/心跳/fencing 语义，只是 claim 者
从进程内 worker 换成本插件后端（经桥端点）：

- claim → daemon 心跳线程（interval = lease/3）；单次心跳网络失败只重试，
  平台明确拒绝或连续失败超过租约时长才判租约丢失；
- 逐条目推进 ``ItemTask``；租约丢失 → **安静放弃**（不报告、不取消平台
  导入、不清理、不做配额动作——新 owner 接管；在途条目停在可续跑点）；
- 取消：heartbeat 回带 ``cancel_requested`` → 剩余条目报告 cancelled、
  在途条目走取消收口（经 ItemTask.should_cancel）；
- 条目报告带 fence（lease_token）：被拒 → LeaseLostError → 安静放弃。
"""

from __future__ import annotations

import threading

from . import errors, states
from .item_task import ItemContext, ItemTask

#: 插件任务序 → 平台条目 stage 白名单（baidu_import_store
#: _PLUGIN_ITEM_STAGES：queued/transferring/downloading/validating/
#: converting/ingesting/ready/failed/cancelled）。插件自身序（§3.2
#: done/awaiting_receipt/cleanup_pending 等）是插件侧状态，回写时映射。
_PLATFORM_STAGE_MAP = {
    states.QUEUED: "queued",
    states.DOWNLOADING: "downloading",
    states.TRANSFORMING: "converting",
    states.VALIDATING: "validating",
    states.DELIVERING: "ingesting",
    states.AWAITING_RECEIPT: "ingesting",
    states.CLEANUP_PENDING: "ingesting",
    states.DONE: "ready",
    states.FAILED: "failed",
    states.CANCELLED: "cancelled",
}


class BatchDriver:
    """一次 claim → 推进 →（平台侧按条目报告收敛批次终态）。"""

    def __init__(self, ctx: ItemContext, *, hooks=None):
        self.ctx = ctx
        #: 测试注入点：on_claim(claim) / on_item_outcome(outcome) /
        #: on_abandon(reason)
        self.hooks = dict(hooks or {})

    def _fire(self, name, *args):
        hook = self.hooks.get(name)
        if hook:
            hook(*args)

    def run_once(self):
        """领一个批次推进；无批次返回 None，有批次返回执行摘要。"""
        try:
            claim = self.ctx.platform.baidu_claim()
        except errors.ContractError as e:
            if e.code == "unauthorized":
                return {"claim": None, "stopped": "plugin_disabled"}
            raise
        if claim is None:
            return None
        self._fire("on_claim", claim)
        return self.run_claimed(claim)

    def run_claimed(self, claim):
        batch = claim.get("batch") or {}
        items = claim.get("items") or []
        batch_id = str(batch.get("batch_id") or "")
        lease_token = str(batch.get("lease_token") or "")
        cfg = self.ctx.config

        stop = threading.Event()   # 置位 = 停心跳（本轮结束）
        lease_lost = threading.Event()
        cancel_flag = {"v": bool(batch.get("cancel_requested"))}
        hb = None
        if lease_token:
            hb = threading.Thread(
                target=self._heartbeat_loop,
                args=(batch_id, lease_token, cfg.heartbeat_interval,
                      cfg.lease_seconds, cancel_flag, stop, lease_lost),
                name="baidu-plugin-heartbeat-%s" % batch_id, daemon=True)
            hb.start()

        outcomes = []
        finished = set()

        def _abandon(reason):
            self._fire("on_abandon", reason)
            return {"batch_id": batch_id, "abandoned": reason,
                    "outcomes": outcomes}

        try:
            for item in items:
                if lease_lost.is_set():
                    return _abandon("lease_lost")
                if cancel_flag["v"]:
                    break
                stage = str(item.get("stage") or "")
                if stage in states.TERMINAL or stage in ("ready", "failed",
                                                         "cancelled"):
                    continue
                task = ItemTask(self.ctx, batch, item)
                try:
                    outcome = task.run(
                        should_cancel=lambda: cancel_flag["v"],
                        should_stop=lease_lost.is_set)
                except errors.LeaseLostError:
                    return _abandon("lease_lost")
                outcomes.append(outcome)
                self._fire("on_item_outcome", outcome)
                if outcome.get("stage") == "stopped":
                    if lease_lost.is_set():
                        return _abandon("lease_lost")
                    return {"batch_id": batch_id,
                            "stopped": outcome.get("error_code"),
                            "outcomes": outcomes}
                # 条目状态回写（带 fence；被拒 → 安静放弃）
                try:
                    self._report(batch_id, lease_token, outcome)
                except errors.LeaseLostError:
                    return _abandon("lease_lost")
                finished.add(str(item.get("item_id")))
            if cancel_flag["v"]:
                # 剩余未开始条目：报告 cancelled（不 begin）；本轮已收口的
                # 条目已按自身结果回写，不得再改成 cancelled
                for item in items:
                    if str(item.get("item_id")) in finished:
                        continue
                    stage = str(item.get("stage") or "")
                    if stage in states.TERMINAL or stage in ("ready",
                                                             "failed",
                                                             "cancelled"):
                        continue
                    try:
                        self.ctx.platform.baidu_report_item(
                            str(item.get("item_id")), batch_id, lease_token,
                            {"stage": states.CANCELLED,
                             "error_code": "batch_cancelled"})
                    except errors.LeaseLostError:
                        return _abandon("lease_lost")
            return {"batch_id": batch_id, "outcomes": outcomes}
        finally:
            stop.set()
            if hb is not None:
                hb.join(timeout=5)

    def _heartbeat_loop(self, batch_id, lease_token, interval, lease_seconds,
                        cancel_flag, stop, lease_lost):
        """daemon 续租（绝不抛出）。平台明确拒绝 → 租约丢失；网络/临时错误
        只在距上次成功续租超过租约时长后才判丢失（此前继续重试）。"""
        import time
        last_ok = time.monotonic()
        while not stop.wait(max(0.05, interval)):
            try:
                _, cancel = self.ctx.platform.baidu_heartbeat(
                    batch_id, lease_token)
            except errors.LeaseLostError:
                lease_lost.set()
                return
            except Exception:  # noqa: BLE001  心跳绝不干扰主流程
                if time.monotonic() - last_ok >= max(1.0, float(lease_seconds)):
                    lease_lost.set()
                    return
                continue
            last_ok = time.monotonic()
            if cancel:
                cancel_flag["v"] = True

    def _report(self, batch_id, lease_token, outcome):
        """条目终态回写（插件阶段 → 平台条目 stage 白名单映射；字段
        白名单仅 stage/error_code——import/slide 绑定经 begin 的
        baidu_item_id，不走 report）。"""
        stage = outcome.get("stage")
        mapped = _PLATFORM_STAGE_MAP.get(stage)
        if mapped is None:
            # stopped（停用/停止请求）：不回写——条目保持 queued 供续跑
            return
        fields = {"stage": mapped}
        if outcome.get("error_code"):
            fields["error_code"] = str(outcome["error_code"])[:500]
        self.ctx.platform.baidu_report_item(
            str(outcome.get("item_id")), batch_id, lease_token, fields)
