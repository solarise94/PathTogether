# -*- coding: utf-8 -*-
"""单条目编排（C5-B 核心）：下载→转换→校验→交付→回执→清理。

插件任务序（§3.2/§7.3，journal 持久化，重启续跑 T12）：

::

    queued → downloading → transforming → validating → delivering
           → awaiting_receipt → cleanup_pending → done

要点（对应合同行）：

- **§4 容量**：begin 带初始 scratch（下载副本字节数）；进入转换前补占
  峰值（源+产物近似 2×源）；交付前按实际产物大小 final 补占（转换产物
  大小不可预知：begin 的 declared_size 取 native 精确值 / convert 最小值 1，
  交付前 topup 到实际值——topup 是 §4.3 的合同机制，只增不减）。任一
  413（配额拒绝）即停：**不再发任何 write**，转失败清理路径。
- **§1.3/§1.5 断线恢复**：每块确认后 journal 落 offset（fsync）；409
  offset_conflict → 读 status 取权威 confirmed_offset 续传（无重复块、
  无空洞）；重启从 journal + status 权威 offset 续传，**绝不重复 begin**。
- **§1.4 回执**：commit 响应丢失（TransportError.response_lost）→
  awaiting_receipt 经 status 读回终态回执（不重发 commit 语义）。
- **§8 裁决 6**：422 declared_checksum_mismatch → 不再重传；取消任务
  （释放 staging 责任）→ 失败收口。
- **§2.4**：401（插件停用）→ 条目停在非终态（可再续跑）；grant 撤销
  （403 import_grant_invalid）→ 失败收口 + 清理仍可用（write_token）。
- **§5/§7.3 清理**：可靠回执 → 停写（关闭文件句柄）→ 删受管根 →
  cleanup-confirm 幂等确认（退避重试）；cleanup_failed 保留已发布结果，
  绝不重传/重 begin。百度网盘侧仅清理本条目副本（ready 项；镜像平台
  _cleanup_copies 口径），绝不触碰云源分享文件。
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

from . import cleanup as cleanup_mod
from . import errors, states
from .convert import ConvertError, classify, find_channel_json, \
    sniff_artifact

_IDEMPOTENCY_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def idempotency_key_for(installation_id, batch_id, item_id, attempt=0):
    """业务幂等键（§7.3「同一个插件 job 重放」）：稳定、可重放、
    受 [A-Za-z0-9_-]{1,128} 约束。attempt>0 = 平台 retry_items 重排后的
    新一次尝试（本地已收口的失败/取消记录数）。"""
    raw = "baidu-%s-%s-%s" % (installation_id, batch_id, item_id)
    key = _IDEMPOTENCY_SAFE.sub("_", raw)
    suffix = "-r%d" % int(attempt) if attempt else ""
    return key[:128 - len(suffix)] + suffix


class ItemContext:
    """条目编排共享依赖（driver 构造一次，条目间复用）。"""

    def __init__(self, config, platform, source, converter, journal, grants,
                 *, hooks=None):
        self.config = config
        self.platform = platform
        self.source = source
        self.converter = converter
        self.journal = journal
        self.grants = grants
        #: 测试注入点（生产为空）：on_stage(stage) / after_write(offset) /
        #: before_commit() / on_cleanup_attempt(...)
        self.hooks = dict(hooks or {})

    def fire(self, name, *args):
        hook = self.hooks.get(name)
        if hook:
            hook(*args)


class ItemTask:
    """单条目一次执行（可多次进入：resume 由 journal 记录驱动）。"""

    def __init__(self, ctx, batch, item):
        self.ctx = ctx
        self.batch = batch
        self.item = item
        self.item_id = str(item["item_id"])
        self.batch_id = str(batch["batch_id"])
        self.name = str(item["name"])
        self.source_size = int(item.get("source_size") or 0)
        self._attempt = 0
        self._last_outcome = None

    # -- 结果 ------------------------------------------------------------ #

    def _outcome(self, stage, error_code=None, import_id=None,
                 slide_id=None, cleanup_status=None):
        return {"item_id": self.item_id, "stage": stage,
                "error_code": error_code, "import_id": import_id,
                "slide_id": slide_id, "cleanup_status": cleanup_status}

    # -- 主入口 ---------------------------------------------------------- #

    def run(self, should_cancel=None, should_stop=None):
        """推进条目到终态（或可续跑的停止点）。返回 outcome dict。

        should_cancel：批次取消 → 取消平台导入并清理。should_stop：租约丢失
        → 在阶段边界/分块之间停下，**不**碰平台导入与受管根（新 owner 续跑）。
        """
        self._should_stop = should_stop or (lambda: False)
        try:
            return self._run(should_cancel)
        except errors.ContractError as e:
            if e.code == "unauthorized":
                # §2.4 插件停用：停止推进；条目保持可续跑（非终态）
                return self._outcome("stopped", error_code="plugin_disabled")
            raise
        except errors.StopRequested:
            return self._outcome("stopped", error_code="stop_requested")

    def _checkpoint(self):
        if self._should_stop():
            raise errors.StopRequested()

    def _run(self, should_cancel):
        cfg = self.ctx.config
        rec = self.ctx.journal.by_item(self.item_id)

        if rec is None:
            finished = self.ctx.journal.finished_for_item(self.item_id)
            # 本地已发布（平台侧未收到回写，如报告时断网）：只重放结果，
            # 必要时续做未确认的清理——绝不因 write_token 已抹除而改判失败。
            # 平台 retry_items 只重排 failed 项，已发布项不会回到 queued。
            for prev in finished:
                if (prev.get("receipt") or {}).get("state") in (
                        "published", "done"):
                    return self._replay_terminal(prev)
            # 此前的失败/取消尝试：先把未收口的清理做完，再以新幂等键
            # 开始下一次尝试（同键 begin 只会重放已终态任务）
            for prev in finished:
                self._settle_previous_attempt(prev)
            self._attempt = len(finished)

        # ---- queued：grant + 分类 + begin（或复用 journal 记录） ------- #
        if rec is None or str(rec.get("import_id") or
                              "").startswith("pending-"):
            grant_id = self.ctx.grants.grant_for(
                self.batch.get("target_project_id"))
            if not grant_id:
                self._last_outcome = self._simple_fail("grant_missing")
                return self._last_outcome
            try:
                info = classify(self.name)
            except ConvertError as e:
                self._last_outcome = self._simple_fail(e.code)
                return self._last_outcome
            self._info = {**info, "grant_id": grant_id}
            rec = self._begin(pending=rec)
            if rec is None:
                return self._last_outcome
        import_id = str(rec["import_id"])
        if not rec.get("write_token"):
            # begin 幂等重放未返回 write_token 且本地无记录 → 不可续
            return self._fail(rec, "write_token_lost")
        root = cfg.managed_root(import_id)
        root.mkdir(parents=True, exist_ok=True)

        stage = rec.get("stage") or states.QUEUED

        # ---- downloading ------------------------------------------------ #
        self._checkpoint()
        if stage in (states.QUEUED, states.DOWNLOADING):
            if not self._download_stage(rec, root):
                return self._last_outcome
            stage = states.TRANSFORMING if rec["needs_convert"] \
                else states.VALIDATING
            if should_cancel and should_cancel():
                return self._cancel(rec)

        # ---- transforming（仅 convert-required）----------------------- #
        self._checkpoint()
        if stage == states.TRANSFORMING:
            if not self._transform_stage(rec, root):
                return self._last_outcome
            stage = states.VALIDATING
            if should_cancel and should_cancel():
                return self._cancel(rec)

        # ---- validating ------------------------------------------------- #
        self._checkpoint()
        if stage == states.VALIDATING:
            if not self._validate_stage(rec, root):
                return self._last_outcome
            stage = states.DELIVERING
            if should_cancel and should_cancel():
                return self._cancel(rec)

        # ---- delivering → awaiting_receipt ------------------------------ #
        self._checkpoint()
        if stage in (states.DELIVERING, states.AWAITING_RECEIPT):
            if not rec.get("receipt"):
                if not self._deliver_stage(rec, root):
                    return self._last_outcome
            if not self._receipt_stage(rec):
                return self._last_outcome
            stage = states.CLEANUP_PENDING

        # ---- cleanup_pending（成功/失败/取消统一经过）-------------------- #
        return self._cleanup_stage(rec, root, cancel_requested=False)

    def _begin(self, pending=None):
        """begin（先落 begin_pending 记录收窄崩溃窗口）。"""
        cfg = self.ctx.config
        cls = self._info
        # native：declared_size = 源大小（枚举精确值）；convert：产物大小
        # 不可预知 → 最小值 1，交付前按实际 topup（§4.3 只增不减）
        declared = 1 if cls["needs_convert"] else max(1, self.source_size)
        # 崩溃续跑沿用先行记录里的键（begin 可能已被平台受理）
        key = (pending or {}).get("idempotency_key") or idempotency_key_for(
            cfg.installation_id, self.batch_id, self.item_id,
            attempt=self._attempt)
        pending_id = "pending-%s" % self.item_id
        rec = self.ctx.journal.begin_pending(
            item_id=self.item_id, batch_id=self.batch_id,
            grant_id=cls["grant_id"],
            project_id=self.batch.get("target_project_id"),
            idempotency_key=key, filename=cls["filename"],
            format_ext=cls["format_ext"], declared_size=declared,
            source_name=self.name, source_size=self.source_size,
            needs_convert=cls["needs_convert"])
        self.ctx.fire("before_begin")
        try:
            resp = self.ctx.platform.import_begin(
                grant_id=cls["grant_id"],
                project_id=self.batch.get("target_project_id"),
                filename=cls["filename"], format_ext=cls["format_ext"],
                declared_size=declared, scratch_bytes=self.source_size,
                profile={"source": "baidu", "plugin_stage": "queued"},
                idempotency_key=key,
                baidu_item_id=self.item_id)
        except errors.ContractError as e:
            code = e.code
            if code == "import_grant_invalid":
                code = "import_grant_invalid:%s" % (e.reason or "unknown")
            self._last_outcome = self._simple_fail(code)
            return None
        self.ctx.fire("after_begin_response", resp)
        import_id = str(resp.get("import_id") or "")
        if not import_id:
            self._last_outcome = self._simple_fail("begin_response_invalid")
            return None
        rec = self.ctx.journal.attach_import(
            pending_id, import_id=import_id,
            write_token=str(resp.get("write_token") or ""),
            chunk_max_bytes=int(resp.get("chunk_max_bytes") or 0) or 67108864,
            slide_id=resp.get("slide_id"))
        return rec

    def _scratch_topup(self, rec, total_bytes):
        """scratch 补占（重复同值幂等）；413 即停（§4.2 不赌）。"""
        try:
            self.ctx.platform.import_scratch(
                rec["import_id"], rec["write_token"],
                total_bytes=int(total_bytes))
            return True
        except errors.ContractError as e:
            if e.code == "unauthorized":
                raise
            self._fail(rec, e.code)
            return False

    def _download_stage(self, rec, root):
        self._set_stage(rec, states.DOWNLOADING)
        src_path = root / "source" / self.name
        if not (src_path.is_file()
                and src_path.stat().st_size == self.source_size):
            if not self._scratch_topup(rec, self.source_size):
                return False
            try:
                self._transfer_and_download(src_path)
                self._download_companion(src_path, root)
            except errors.SourceError as e:
                self._fail(rec, e.code)
                return False
            if not (src_path.is_file()
                    and src_path.stat().st_size == self.source_size):
                self._fail(rec, "size_mismatch")
                return False
        return True

    def _transfer_and_download(self, src_path):
        """转存+下载（含崩溃对账：副本已在且大小一致 → 不重转存）。

        fs_id 缺失（平台条目视图未下发）且无既有副本 → 条目失败
        ``fs_id_missing``（显式可见，不静默跳过——平台侧补齐 fs_id 前
        该条目无法转存）。
        """
        batch = self.batch
        copies = {c["name"]: c for c in
                  self.ctx.source.list_batch_copies(self.batch_id)}
        copy = copies.get(self.name)
        if copy is None or int(copy["size"]) != self.source_size:
            fs_id = self.item.get("fs_id")
            if not fs_id:
                raise errors.SourceError("fs_id_missing",
                                         "条目视图缺 fs_id，无法转存")
            self.ctx.source.transfer_selected(
                self.batch_id, batch.get("share_url"),
                batch.get("extraction_code"), [str(fs_id)])
        self.ctx.source.download_to(
            "%s/%s" % (self.batch_id, self.name), src_path.parent)

    def _download_companion(self, src_path, root):
        """KFBF 伴随 channel.json（C0 清点 §4：``<stem>_kfbf/Annotations/``）。

        桥条目可带 ``companion_fs_id``/``companion_name``（平台枚举发现
        伴随文件时下发）；缺省 → 跳过（转换按文件自身元数据）。伴随文件
        落在源文件旁，使 :func:`find_channel_json` 能指认。
        """
        if not self.name.lower().endswith(".kfbf"):
            return
        comp_fs = str(self.item.get("companion_fs_id") or "")
        if not comp_fs:
            return
        comp_name = str(self.item.get("companion_name") or "channel.json")
        target_dir = src_path.parent / (src_path.stem + "_kfbf") / \
            "Annotations"
        if (target_dir / "channel.json").is_file():
            return
        copies = {c["name"] for c in
                  self.ctx.source.list_batch_copies(self.batch_id)}
        if comp_name not in copies:
            self.ctx.source.transfer_selected(
                self.batch_id, self.batch.get("share_url"),
                self.batch.get("extraction_code"), [comp_fs])
        tmp_dir = root / "source" / ".companion-tmp"
        self.ctx.source.download_to(
            "%s/%s" % (self.batch_id, comp_name), tmp_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(tmp_dir / comp_name),
                    str(target_dir / "channel.json"))

    def _transform_stage(self, rec, root):
        self._set_stage(rec, states.TRANSFORMING)
        out_path = root / "output" / rec["filename"]
        src_path = root / "source" / self.name
        if not (out_path.is_file() and rec.get("artifact_size")
                and out_path.stat().st_size == rec["artifact_size"]):
            # §4.2：写下一阶段前补占 scratch（峰值近似 2×源）
            if not self._scratch_topup(rec, self.source_size * 2):
                return False
            channel = find_channel_json(src_path)
            try:
                self.ctx.converter.convert(src_path, out_path, channel)
            except ConvertError as e:
                self._fail(rec, e.code)
                return False
        return True

    def _validate_stage(self, rec, root):
        self._set_stage(rec, states.VALIDATING)
        artifact = self._artifact_path(rec, root)
        try:
            size = artifact.stat().st_size
        except OSError:
            self._fail(rec, "artifact_missing")
            return False
        if size <= 0:
            self._fail(rec, "artifact_empty")
            return False
        try:
            sniff = sniff_artifact(artifact)
        except ConvertError as e:
            self._fail(rec, e.code)
            return False
        digest = _sha256_file(artifact)
        rec.update(self.ctx.journal.save({
            "import_id": rec["import_id"],
            "artifact_rel": str(artifact.relative_to(root)),
            "artifact_size": size, "artifact_sha256": digest,
            "artifact_sniff": sniff}))
        return True

    def _artifact_path(self, rec, root):
        rel = rec.get("artifact_rel")
        if rel:
            return root / rel
        if rec["needs_convert"]:
            return root / "output" / rec["filename"]
        return root / "source" / rec["source_name"]

    def _deliver_stage(self, rec, root):
        self._set_stage(rec, states.DELIVERING)
        import_id = rec["import_id"]
        token = rec["write_token"]
        artifact = self._artifact_path(rec, root)
        size = artifact.stat().st_size
        digest = rec.get("artifact_sha256") or _sha256_file(artifact)
        declared = int(rec.get("declared_size") or 0)
        if size > declared:
            # §4.3：final 补占（转换产物实际大小；413 即停不传）
            try:
                self.ctx.platform.import_topup(
                    import_id, token, size - declared)
            except errors.ContractError as e:
                if e.code == "unauthorized":
                    raise
                self._fail(rec, e.code)
                return False
            rec.update(self.ctx.journal.save({"import_id": import_id,
                                              "declared_size": size}))
            declared = size
        # 续传起点：journal 记录 + 平台权威 confirmed_offset（T12/§1.3）
        try:
            st = self.ctx.platform.import_status(import_id, token)
        except errors.ContractError as e:
            if e.code == "unauthorized":
                raise
            self._fail(rec, e.code)
            return False
        state = st.get("state")
        if state in ("published", "done"):
            # 上次已收口（崩溃在回执后、journal 前）：直接进回执处理
            rec.update(self.ctx.journal.save({"import_id": import_id,
                                              "receipt": st}))
            return True
        if state not in ("created", "writing"):
            self._fail(rec, "import_state_invalid:%s" % state)
            return False
        offset = int(st.get("confirmed_offset") or 0)
        chunk_max = int(rec.get("chunk_max_bytes") or 0) or 67108864
        with open(artifact, "rb") as fh:
            while offset < size:
                self._checkpoint()
                fh.seek(offset)
                chunk = fh.read(min(chunk_max, size - offset))
                if not chunk:
                    self._fail(rec, "artifact_truncated")
                    return False
                try:
                    resp = self.ctx.platform.import_write_chunk(
                        import_id, token, offset, chunk)
                except errors.ContractError as e:
                    if e.code == "offset_conflict":
                        # §1.3：读 status 取权威 offset 续传（无重复块）
                        st2 = self.ctx.platform.import_status(
                            import_id, token)
                        offset = int(st2.get("confirmed_offset") or 0)
                        continue
                    if e.code == "checksum_mismatch":
                        # 块作废可重发（§1.3 第 5 步）：重读重发一次
                        fh.seek(offset)
                        retry = fh.read(len(chunk))
                        if retry != chunk:
                            self._fail(rec, "artifact_changed")
                            return False
                        resp = self.ctx.platform.import_write_chunk(
                            import_id, token, offset, retry)
                    else:
                        if e.code == "unauthorized":
                            raise
                        self._fail(rec, e.code)
                        return False
                offset = int(resp.get("confirmed_offset")
                             or offset + len(chunk))
                rec.update(self.ctx.journal.save({
                    "import_id": import_id, "delivered_offset": offset}))
                self.ctx.fire("after_write", offset)
        # §1.4 commit（同步端点；declared_sha256 交叉核对）
        self.ctx.fire("before_commit")
        try:
            receipt = self.ctx.platform.import_commit(
                import_id, token, digest)
        except errors.TransportError as e:
            if not e.response_lost:
                raise
            receipt = None  # awaiting_receipt 经 status 读回
        except errors.ContractError as e:
            if e.code == "unauthorized":
                raise
            # §8 裁决 6：声明不符 → 422，无 intent、平台侧仍 writing；
            # 插件取消任务收口（不重传）
            self._fail(rec, e.code)
            return False
        rec.update(self.ctx.journal.save({
            "import_id": import_id, "stage": states.AWAITING_RECEIPT,
            "receipt": receipt, "declared_sha256": digest}))
        return True

    def _receipt_stage(self, rec):
        """回执确认（丢失 → status 轮询直到终态）。"""
        self._set_stage(rec, states.AWAITING_RECEIPT)
        import_id, token = rec["import_id"], rec["write_token"]
        receipt = rec.get("receipt")
        if receipt and receipt.get("state") in ("published", "done",
                                                "cancelled", "failed"):
            return True
        cfg = self.ctx.config
        for _ in range(max(1, cfg.receipt_poll_max)):
            st = self.ctx.platform.import_status(import_id, token)
            state = st.get("state")
            if state in ("published", "done"):
                rec.update(self.ctx.journal.save({"import_id": import_id,
                                                  "receipt": st}))
                return True
            if state in ("failed", "cancelled"):
                self._fail(rec, st.get("fail_code")
                           or ("cancelled" if state == "cancelled"
                               else "failed"))
                return False
            time.sleep(cfg.receipt_poll_seconds)
        self._fail(rec, "receipt_timeout")
        return False

    def _cleanup_stage(self, rec, root, cancel_requested):
        """统一清理收口（成功/失败/取消都过这里；§5/§7.3）。"""
        import_id = rec["import_id"]
        token = rec.get("write_token") or ""
        receipt = rec.get("receipt") or {}
        published = receipt.get("state") in ("published", "done")
        if not token:
            # 无任务级凭证（begin 重放拿不回 write_token 的窄窗口）：
            # 平台侧动作（cancel/confirm）不可达——本地清理照做，平台
            # 侧由 PRODUCER_IMPORT_MAX_AGE 超期 sweep 收口（§3.1）。
            try:
                cleanup_mod.remove_tree(root)
            except OSError:
                pass
            status = "no_token"
        else:
            if not published:
                # 未发布：取消任务（幂等；§2.4 grant 撤销后清理仍可用——
                # 取消被拒不阻断清理路径）
                try:
                    self.ctx.platform.import_cancel(import_id, token)
                except errors.ContractError as e:
                    if e.code == "unauthorized":
                        raise
                except errors.TransportError:
                    pass
            self._set_stage(rec, states.CLEANUP_PENDING)
            if published:
                # §6.2 源副本清理：仅本条目（ready 项）的网盘副本；绝不
                # 触碰云端分享源文件/用户本机原件
                try:
                    self.ctx.source.cleanup_batch_copies(
                        self.batch_id,
                        ["%s/%s" % (self.batch_id, self.name)])
                except errors.SourceError:
                    pass  # 副本清理失败不回滚 ready；批次级对账兜底
            cfg = self.ctx.config
            status = cleanup_mod.cleanup_managed_root(
                self.ctx.platform, import_id, token, root,
                max_attempts=cfg.cleanup_max_attempts,
                backoff_base=cfg.cleanup_backoff_base)
        terminal = states.DONE
        error_code = None
        if not published:
            kind = rec.get("terminal") or {}
            if kind.get("kind") == "cancelled" or cancel_requested:
                terminal = states.CANCELLED
            else:
                terminal = states.FAILED
                error_code = kind.get("code")
        # cleanup_failed（§7.3）：保留已发布结果与清理状态（不重传）；
        # 状态经 outcome.cleanup_status 暴露。write_token 仅在清理确认
        # 成功后抹除——cleanup_failed 的任务仍需任务级凭证重试清理
        #（§2.3 第 3 层：token 与任务同寿命）
        rec = self.ctx.journal.save({
            "import_id": import_id, "stage": terminal,
            "terminal": {"kind": terminal if terminal != states.DONE
                         else "done", "code": error_code},
            "cleanup_status": status, "receipt": receipt or None})
        if status == "cleaned":
            self.ctx.journal.strip_secret(import_id)
        out = self._outcome(terminal, error_code=error_code,
                            import_id=import_id,
                            slide_id=receipt.get("slide_id"),
                            cleanup_status=status)
        self._last_outcome = out
        return out

    def _settle_previous_attempt(self, rec):
        """此前未发布的尝试若清理未确认（含收口途中崩溃），先补做
        （取消 + 受管根清理），不产生本条目的 outcome。"""
        done = rec.get("stage") in states.TERMINAL and rec.get(
            "cleanup_status") in ("cleaned", "no_token")
        if done or not rec.get("write_token"):
            return
        saved = self._last_outcome
        self._cleanup_stage(rec, self.ctx.config.managed_root(
            rec["import_id"]), cancel_requested=False)
        self._last_outcome = saved

    def _replay_terminal(self, rec):
        import_id = rec["import_id"]
        receipt = rec.get("receipt") or {}
        status = rec.get("cleanup_status")
        token = rec.get("write_token") or ""
        if status not in ("cleaned", "no_token") and token:
            cfg = self.ctx.config
            status = cleanup_mod.cleanup_managed_root(
                self.ctx.platform, import_id, token,
                cfg.managed_root(import_id),
                max_attempts=cfg.cleanup_max_attempts,
                backoff_base=cfg.cleanup_backoff_base)
            self.ctx.journal.save({"import_id": import_id,
                                   "cleanup_status": status})
            if status == "cleaned":
                self.ctx.journal.strip_secret(import_id)
        kind = rec.get("terminal") or {}
        out = self._outcome(rec["stage"], error_code=kind.get("code"),
                            import_id=import_id,
                            slide_id=receipt.get("slide_id"),
                            cleanup_status=status)
        self._last_outcome = out
        return out

    # ---- 失败/取消/状态辅助 --------------------------------------------- #

    def _set_stage(self, rec, stage):
        if (rec.get("stage") or states.QUEUED) != stage:
            rec.update(self.ctx.journal.save({"import_id": rec["import_id"],
                                              "stage": stage}))
        self.ctx.fire("on_stage", stage)

    def _fail(self, rec, code):
        """失败收口（过清理）。code 不进 message（脱敏）。"""
        iid = rec.get("import_id") if rec else None
        if iid:
            rec = self.ctx.journal.save({
                "import_id": iid,
                "terminal": {"kind": "failed", "code": code}})
            out = self._cleanup_stage(rec, self.ctx.config.managed_root(iid),
                                      cancel_requested=False)
            self._last_outcome = out
            return out
        self._last_outcome = self._simple_fail(code)
        return self._last_outcome

    def _simple_fail(self, code):
        """无 import（begin 前）失败：无受管根/无清理确认，直接报告。"""
        self._last_outcome = self._outcome(states.FAILED, error_code=code)
        return self._last_outcome

    def _cancel(self, rec):
        iid = rec.get("import_id")
        if not iid:
            self._last_outcome = self._outcome(states.CANCELLED)
            return self._last_outcome
        rec = self.ctx.journal.save({"import_id": iid,
                                     "terminal": {"kind": "cancelled",
                                                  "code": None}})
        out = self._cleanup_stage(rec, self.ctx.config.managed_root(iid),
                                  cancel_requested=True)
        self._last_outcome = out
        return out
