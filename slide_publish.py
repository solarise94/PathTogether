# -*- coding: utf-8 -*-
"""slide_publish —— 统一发布编排（slide ID 化重构 P1-A 骨架、P3 接线）。

本模块是「代码旁合同」：发布流程、锁顺序、取消/恢复裁决镜像自
docs/slide-id-refactor-p3-contract-20260925.md §3、
docs/slide-id-refactor-p1-contract-20260925.md §3.3/§4/§5 与
docs/slide-id-storage-refactor-agent-plan-20260925.md §3。
worker 可 import 本模块（不得 import app）；文件系统操作全部委托
slide_storage，DB 状态/CAS 全部委托 slide_store/upload_task_store/upload_guard
——本模块只做编排。

publish_slide 合同（步骤顺序，P3 接线实现；P4-b 起步骤 0/2/4 的任务读写
经 ``PublishChannel`` 协议注入——upload_tasks 是默认通道，COS ingestion 经
``publish_with_channel`` + ingestion 通道接入，发布编排保持单一实现）：

  0. 前置验证：task_ref 指向的任务行存在且归属本 owner；generation 是任务
     当前有效代次（intent.generation == 入参 generation；旧 generation 不得
     发布/结算——plan §3.2）；slide_id = 任务绑定（upload_tasks.slide_id 唯一
     绑定源）；资产 owner 与任务 owner 一致（元数据 owner 不匹配说明不变量
     被破坏，应隔离并告警，**绝不自动修正 owner**，plan §3.2）。
  1. intent 已由调用方随 begin_commit/begin_legacy_commit 的 CAS **同事务**
     持久化（upload_tasks.commit_intent_json，0068）：task_ref、generation、
     slide_id、owner、manifest、哈希、实际结算字节——保证崩溃恢复可判定
     「全未发布（回滚）/ 已发布（幂等收口）/ 证据冲突（fail-closed 告警）」。
     本模块读取并复核 intent 与入参的一致性，不重新生成。
  2. 取锁（锁序第一把）：``pg_advisory_xact_lock(hashtext('slide:' ||
     slide_id))``（slide_store.acquire_slide_lock）——所有 publish/delete/
     recovery 的跨进程仲裁点；进程内 mutex 不够（plan §3.1-4）。锁内重验：
     任务仍在 committing 且 token/generation 未变、资产仍 staging、任务未
     取消（cancel 对 committing 拒绝——取消先赢只发生在受理前）、预约仍
     有效（renew 后仍 reserved）。取消先赢则不能发布；提交已进入不可撤销
     段（intent 落库）则返回稳定错误并由恢复继续完成/恢复（plan §3.2）。
  3. 文件系统发布：slide_storage.publish_bundle_no_clobber（同卷原子
     rename / 跨卷私有暂存+完整复制校验；fsync 文件与目录；目标已存在即
     FileExistsError，绝不覆盖）。完整包发布，不能入口先可读、伴侣后到。
     **崩溃恢复幂等分支**：目标 objects/<slide_id>/ 已存在 →
     slide_storage.verify_bundle 逐文件核对 manifest 的 size/sha256——吻合
     → 跳过 FS 只做第 4 步 DB 收口；不吻合 → ``PublishConflict``（fail-closed
     告警，不删不猜——绝不按名称或 SHA 收养别人的资产）。
  4. 短事务 CAS + 结算（plan §3.1-6；锁序：advisory → 任务行 FOR UPDATE
     → slides 行 → upload_reservations → upload_user_quotas（→ cos_pool_state））：
     slide_store.mark_ready（expected_state=staging, accounted_bytes=实际字节）
     + slide_store.record_revision（slide_assets 写内容 revision：sha256 前缀）
     与 consume reservation、任务 committed + 清 commit_intent **同一事务**；
     任一步失败整体回滚（FS 已发布、DB 未提交 → 不可见，恢复重试收口）。
     幂等：任务已 committed 且 intent 已清 → 返回现状（重复调用/响应丢失
     重试只结算一次——consume 幂等 + 状态机单次转移 + accounted_bytes 幂等键）。
  5. 第 4 步提交后资产才可见：DB ready 是唯一可见性开关；FS 已发布、DB
     未提交的内容依然不可读（authorize_read 拒绝）。

取消/失败收口（合同 §3.3 第 8 步，由 app 侧通道层执行；本模块提供判定）：
清理 staging 目录**后**再释放 reservation（plan §3.3：清理确认后释放）；
staging 资产行 → failed（保留证据）。预约失效（ReservationInvalid）发生在
收口事务内 → 整体回滚，由恢复路径撤回已发布包（remove_bundle）+ fail-closed。

配额与回收口径（plan §3.3）：ready 发布与 used_bytes 增加同事务且只发生一次
（consume 幂等 + accounted_bytes 稳定值）；删除先置 deleting 再物理清理，
清理成功后按 accounted_bytes 幂等减少实占（upload_guard.refund_used_bytes_
locked，P3 删除端点接线）；重复调用/worker 重试不得重复减账。

generation 语义（合同 §3.2）：V2 = commit 受理的 commit_token（begin_commit
生成并注入 intent——任务行可重判的代次；传输阶段 staging 固定 "transfer"，
受理时原子搬入 .staging/<task_id>/<token>/）；V1 原生单文件 = "1"。
"""

from __future__ import annotations

import json
import logging

import pg_store

import slide_storage
import slide_store
import upload_guard
import upload_task_store

_LOG = logging.getLogger(__name__)


class PublishError(Exception):
    """发布编排业务异常（调用方按 code 映射稳定错误响应/恢复路径）。

    ``deterministic`` = True 表示重试无意义（证据冲突/状态破坏）；False 为
    临时故障（保持 committing，由恢复路径重试）。
    """

    def __init__(self, code, message, *, deterministic=True, task=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.deterministic = bool(deterministic)
        self.task = task


class PublishConflict(PublishError):
    """目标已存在但 manifest/sha 不吻合——不变量破坏，fail-closed 告警不猜。"""

    def __init__(self, message, task=None):
        super().__init__("publish_conflict", message,
                         deterministic=True, task=task)


def _connect():
    import psycopg
    conn = pg_store.connect()
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _decode_intent(task):
    """任务行 → intent dict；None=无 intent（未受理/已收口）；{}=损坏（冲突）。"""
    return upload_task_store.decode_commit_intent(task.get("commit_intent_json"))


# --------------------------------------------------------------------------- #
# channel 适配缝（P4-b 合同 §5.4）：任务族差异注入点
# --------------------------------------------------------------------------- #
class PublishChannel:
    """六步编排的任务通道适配协议（P4-b；P3 的 publish_slide 现为
    upload_tasks 专用，其它任务族经本协议注入，**发布编排不得复制进 worker**）。

    步骤 0/2/4 中随任务表变化的读写全部收敛为下列钩子（锁序、no-clobber FS、
    恢复幂等、结算短事务结构仍是本模块的单一实现）：

      - ``load_task(task_ref)`` → 任务快照 dict | None（步骤 0 前置读取；
        None=任务不存在）。
      - ``is_settled(task)`` → bool（已收口——重复调用/响应丢失重试的幂等出口）。
      - ``decode_intent(task)`` → None（无 intent）/ ``{}``（损坏，调用方
        fail-closed）/ intent dict（步骤 1 复核；本模块读取不重新生成）。
      - ``task_commit_token(task)`` → 任务行当前代次凭证，与
        ``intent["commit_token"]`` 比对（upload_tasks=commit_token；
        ingestion=worker_generation）。
      - ``precheck_locked(cur, task_ref, generation, slide_id, owner_user_id,
        intent)`` → 锁内重验（步骤 2；调用方事务内、advisory 锁已由调用方
        **在本事务最先**取得）。违规抛 PublishError / 通道 fencing 异常
        （如 ingestion 的 StaleLease——由通道调用方按其 worker 语义处理）。
        预约有效性在本钩子内重验（renew 后仍 reserved；失效抛
        upload_guard.ReservationInvalid 整体回滚）。
      - ``settle(task_ref, generation, slide_id, sha256, accounted_bytes)``
        → ``(task_after, already_settled)``（步骤 4 短事务；内部自取
        advisory 锁——**第一把锁**，随后按锁序取任务行 → slides 行 →
        upload_reservations → upload_user_quotas）。

    settle 契约（镜像 upload_tasks 实现）：slides 行 CAS（staging→ready +
    accounted_bytes=实际字节）→ slide_assets 内容 revision → consume
    reservation → 任务行收口 UPDATE，**同一事务**；已收口返回 (task, True)
    不重复结算；任务代次失效拒绝（不猜）。
    """


class UploadTaskPublishChannel(PublishChannel):
    """默认通道：upload_tasks（V2 分片 + V1 原生单文件；P3 行为原样）。"""

    def load_task(self, task_ref):
        return upload_task_store.get_task(task_ref)

    def is_settled(self, task):
        return bool(task) and \
            task.get("state") == upload_task_store.STATE_COMMITTED and \
            task.get("commit_intent_json") is None

    def decode_intent(self, task):
        return _decode_intent(task)

    def task_commit_token(self, task):
        return task.get("commit_token")

    def precheck_locked(self, cur, task_ref, generation, slide_id,
                        owner_user_id, intent):
        return _precheck_locked(cur, task_ref, generation, slide_id,
                                owner_user_id, intent)

    def settle(self, task_ref, generation, slide_id, sha256, accounted_bytes):
        return _settle_publish(task_ref, generation, slide_id, sha256,
                               accounted_bytes)


#: 默认通道单例（publish_slide 用；无状态可安全共享）。
UPLOAD_TASK_CHANNEL = UploadTaskPublishChannel()


def build_manifest(entry, size, sha256):
    """单文件包 manifest 构造（entry=data.<ext>；files 含 size/sha256）。"""
    return {"entry": entry, "files": [{"path": entry, "size": int(size),
                                       "sha256": str(sha256).lower()}]}


def build_intent(slide_id, owner_user_id, manifest, sha256, accounted_bytes):
    """调用方在 begin_commit(intent=…) 时持久化的 intent 载荷（不含
    task_ref/generation/commit_token——由任务存储 CAS 内注入）。"""
    return {
        "slide_id": slide_id,
        "owner_user_id": owner_user_id or "",
        "manifest": manifest,
        "sha256": str(sha256).lower(),
        "accounted_bytes": int(accounted_bytes),
    }


# --------------------------------------------------------------------------- #
# 步骤 0-2：前置验证 + 锁内重验（短事务；advisory 第一把锁）
# --------------------------------------------------------------------------- #
def _precheck_locked(cur, task_ref, generation, slide_id, owner_user_id,
                     intent):
    """锁内重验（合同步骤 0/2）：advisory 已由调用方在**本事务**最先取得。

    返回锁内任务快照；违规抛 PublishError（deterministic 按语义标注）。
    """
    cur.execute("SELECT %s FROM upload_tasks WHERE upload_id = %%s FOR UPDATE"
                % upload_task_store._PG_COLS, (task_ref,))
    row = cur.fetchone()
    if row is None:
        raise PublishError("task_not_found", "任务不存在：%s" % task_ref,
                           deterministic=True)
    task = upload_task_store._norm_row(row)
    if (task.get("state") != upload_task_store.STATE_COMMITTING
            or task.get("commit_token") != intent.get("commit_token")):
        # 已收口（committed+intent 清空）由调用方在进入前判定；此处只剩
        # 取消/被恢复流程回滚/代次漂移——一律拒绝，不猜。
        raise PublishError(
            "generation_mismatch",
            "任务代次失效（state=%r token 匹配=%s）——旧 generation 不得发布"
            % (task.get("state"),
               task.get("commit_token") == intent.get("commit_token")),
            deterministic=True, task=task)
    if (task.get("slide_id") or "") != slide_id:
        raise PublishError("task_slide_mismatch",
                           "任务绑定的资产与本发布不一致", deterministic=True,
                           task=task)
    if str(intent.get("generation")) != str(generation):
        raise PublishError("generation_mismatch",
                           "intent generation 与入参不一致", deterministic=True,
                           task=task)
    task_owner = (task.get("owner_user_id") or "").strip()
    intent_owner = (intent.get("owner_user_id") or "").strip()
    if task_owner != intent_owner:
        raise PublishError(
            "owner_mismatch",
            "任务 owner 与 intent owner 不一致（%r != %r）——不变量破坏，"
            "隔离告警不自动修正" % (task_owner, intent_owner),
            deterministic=True, task=task)
    if owner_user_id is not None and (owner_user_id or "").strip() != task_owner:
        raise PublishError("owner_mismatch",
                           "任务归属与发布发起者不一致（拒绝，不自动修正）",
                           deterministic=True, task=task)
    # 预约有效性（plan §3.1-4：锁内重验；无预约=owner/本地态视为持有）
    rid = task.get("reservation_id")
    if rid:
        out = upload_guard.renew_reservation_locked(cur, rid)
        if not upload_guard.reservation_is_active(out):
            raise upload_guard.ReservationInvalid(
                "预占已失效，不能发布：%r" % rid)
    return task


# --------------------------------------------------------------------------- #
# 步骤 4：短事务 CAS + 结算（advisory → 任务行 → slides → reservations → quotas）
# --------------------------------------------------------------------------- #
def _settle_publish(task_ref, generation, slide_id, sha256, accounted_bytes):
    """发布收口短事务：mark_ready + record_revision + consume + committed +
    清 intent（同一事务；任一步失败整体回滚，恢复重试收口）。

    幂等：任务已 committed 且 intent 已清 → 返回 (task, True)（已收口）。
    ReservationInvalid 向上抛（事务回滚，任务保持 committing——由恢复路径
    撤回已发布包并 fail-closed，不留 committed 文件）。
    """
    conn = _connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                cur.execute(
                    "SELECT %s FROM upload_tasks WHERE upload_id = %%s "
                    "FOR UPDATE" % upload_task_store._PG_COLS, (task_ref,))
                row = cur.fetchone()
                if row is None:
                    raise PublishError("task_not_found",
                                       "任务不存在：%s" % task_ref,
                                       deterministic=True)
                task = upload_task_store._norm_row(row)
                if task.get("state") == upload_task_store.STATE_COMMITTED \
                        and task.get("commit_intent_json") is None:
                    return task, True  # 已收口（重复调用/恢复重入）
                _intent = _decode_intent(task)
                if (task.get("state") != upload_task_store.STATE_COMMITTING
                        or task.get("commit_token") != (_intent or {}).get(
                            "commit_token")
                        or str((_intent or {}).get("generation")) != str(generation)):
                    raise PublishError(
                        "generation_mismatch",
                        "收口时任务代次失效（state=%r）" % task.get("state"),
                        deterministic=True, task=task)
                # slides 行 CAS（expected_state=staging）：ready 与
                # accounted_bytes 同事务设置（R-12：删除结算用它）。锁序：
                # 任务行 → **slides 行** → upload_reservations → quotas。
                cur.execute(
                    "UPDATE slides SET asset_state=%s, published_at=now(), "
                    "accounted_bytes=%s, updated_at=now() "
                    "WHERE slide_id=%s AND asset_state=%s",
                    (slide_store.SlideState.READY, int(accounted_bytes),
                     slide_id, slide_store.SlideState.STAGING))
                if cur.rowcount != 1:
                    cur.execute(
                        "SELECT asset_state, accounted_bytes FROM slides "
                        "WHERE slide_id=%s", (slide_id,))
                    srow = cur.fetchone()
                    if not (srow
                            and srow["asset_state"] == slide_store.SlideState.READY
                            and srow["accounted_bytes"] is not None
                            and int(srow["accounted_bytes"])
                            == int(accounted_bytes)):
                        raise PublishError(
                            "asset_state_conflict",
                            "资产不在 staging 且非同参 ready（state=%r "
                            "accounted=%r）——不猜" %
                            (srow and srow["asset_state"],
                             srow and srow["accounted_bytes"]),
                            deterministic=True, task=task)
                # 内容 revision（合同 §4：id_bundle 资产 revision =
                # slide_assets 最新行的 sha256 前缀；与收口同事务）。
                slide_store.record_revision(
                    slide_id, "sha256:%s" % str(sha256).lower()[:16],
                    conn=conn)
                rid = task.get("reservation_id")
                if rid:
                    upload_guard.consume_reservation_locked(
                        cur, rid, int(accounted_bytes))
                cur.execute(
                    "UPDATE upload_tasks SET state=%s, sha256_actual=%s, "
                    "commit_intent_json=NULL, updated_at=now() "
                    "WHERE upload_id=%s",
                    (upload_task_store.STATE_COMMITTED,
                     sha256 or None, task_ref))
                cur.execute(
                    "SELECT %s FROM upload_tasks WHERE upload_id = %%s"
                    % upload_task_store._PG_COLS, (task_ref,))
                return upload_task_store._norm_row(cur.fetchone()), False
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 对外编排入口（六步；含崩溃恢复的幂等重跑）
# --------------------------------------------------------------------------- #
def publish_with_channel(task_ref, generation, slide_id, channel,
                         manifest=None, *, owner_user_id=None,
                         upload_root=None):
    """按模块 docstring 合同编排发布（六步），任务族读写经 ``channel`` 注入。

    P4-b：publish_slide 现为 upload_tasks 专用；COS ingestion 等任务族经
    ``channel``（PublishChannel 协议）接入同一编排——发布逻辑单一实现，
    不复制进 worker。

    参数：
      - task_ref：通道任务键（upload_tasks=upload_id；ingestion=job_id）。
      - generation：任务代次（fencing 键）；必须与 intent.generation 一致
        （旧代次拒绝）。
      - slide_id：预分配资产 ID（任务表 slide_id 绑定源）。
      - channel：PublishChannel 协议实现（步骤 0/2/4 的任务读写钩子）。
      - manifest：包清单；None → 从任务 intent 读取（崩溃恢复路径——
        intent 是发布的权威证据，恢复不重新构造）。
      - owner_user_id：任务归属交叉验证（None 跳过该比对，intent/任务/资产
        三方一致性仍验证）。
      - upload_root：上传根（测试隔离用；None → slide_storage 默认解析）。

    返回 (task_after, already_settled)。异常：
      PublishError(deterministic=True) → 证据冲突/状态破坏（不重试）；
      PublishError(deterministic=False) → 临时故障（保持 committing，恢复重试）；
      PublishConflict → 目标已存在且不吻合（fail-closed 告警不猜）；
      通道 fencing 异常（如 ingestion 的 StaleLease）→ 原样上抛；
      upload_guard.ReservationInvalid → 预约失效（事务已回滚，调用方撤回）。
    """
    task = channel.load_task(task_ref)
    if task is None:
        raise PublishError("task_not_found", "任务不存在：%s" % task_ref,
                           deterministic=True)
    if channel.is_settled(task):
        return task, True  # 已收口：重复 commit/响应丢失重试的幂等出口
    intent = channel.decode_intent(task)
    if intent is None:
        raise PublishError("intent_missing",
                           "任务无 publish intent（未受理或已收口）",
                           deterministic=True, task=task)
    if not intent:
        raise PublishConflict("publish intent 损坏（非法 JSON）——fail-closed",
                              task=task)
    if str(intent.get("generation")) != str(generation):
        raise PublishError("generation_mismatch",
                           "入参 generation 与 intent 不一致（旧代次拒绝）",
                           deterministic=True, task=task)
    if (intent.get("slide_id") or "") != slide_id:
        raise PublishError("task_slide_mismatch",
                           "intent slide_id 与入参不一致", deterministic=True,
                           task=task)
    if channel.task_commit_token(task) != intent.get("commit_token"):
        raise PublishError("generation_mismatch",
                           "intent 代次凭证与任务行不一致", deterministic=True,
                           task=task)
    try:
        manifest = slide_storage.validate_manifest(
            manifest if manifest is not None else intent.get("manifest"))
    except ValueError as e:
        raise PublishConflict("manifest 非法：%s" % e, task=task) from e

    desc = slide_store.resolve_slide_id(slide_id)
    if desc is None:
        raise PublishError("asset_missing", "资产行不存在：%s" % slide_id,
                           deterministic=True, task=task)
    if desc.storage_layout != slide_store.StorageLayout.ID_BUNDLE:
        raise PublishError("asset_layout_invalid",
                           "非 id_bundle 资产不走统一发布（layout=%r）"
                           % desc.storage_layout, deterministic=True, task=task)

    bundle_already = False
    if desc.asset_state == slide_store.SlideState.READY:
        # 崩溃恢复幂等分支（合同步骤 3）：目标已存在 → 逐文件核对 manifest。
        if not slide_storage.verify_bundle(slide_id, manifest,
                                           root=upload_root):
            raise PublishConflict(
                "目标包已存在但 manifest/sha 不吻合（slide_id=%s）——"
                "不变量破坏，fail-closed 不删不猜" % slide_id, task=task)
        bundle_already = True
    elif desc.asset_state == slide_store.SlideState.STAGING:
        # 资产仍 staging 但目标已存在（DB 收口前崩溃的恢复）：核对 manifest
        # 吻合 → 只做 DB CAS；不吻合 → fail-closed。否则从 staging 发布。
        if slide_storage.bundle_dir(slide_id, root=upload_root).exists():
            if not slide_storage.verify_bundle(slide_id, manifest,
                                               root=upload_root):
                raise PublishConflict(
                    "目标包已存在且 manifest/sha 不吻合（slide_id=%s）——"
                    "fail-closed 不删不猜" % slide_id, task=task)
            bundle_already = True
        else:
            staging = slide_storage.staging_dir(task_ref, generation,
                                                root=upload_root)
            try:
                slide_storage.publish_bundle_no_clobber(
                    staging, slide_id, manifest, root=upload_root)
            except FileExistsError:
                # no-clobber 撞目标（与状态查询的竞态窗口）：按恢复分支重判。
                if not slide_storage.verify_bundle(slide_id, manifest,
                                                   root=upload_root):
                    raise PublishConflict(
                        "目标包已存在且不吻合（slide_id=%s）——fail-closed"
                        % slide_id, task=task) from None
                bundle_already = True
            except ValueError as e:
                raise PublishError("staging_invalid", str(e),
                                   deterministic=True, task=task) from e
            except OSError as e:
                raise PublishError("staging_io_error", "发布 IO 故障：%s" % e,
                                   deterministic=False, task=task) from e
    else:
        raise PublishError(
            "asset_state_invalid",
            "资产不在 staging/ready（state=%r）——不能发布" % desc.asset_state,
            deterministic=True, task=task)

    sha256 = intent.get("sha256") or ""
    accounted = int(intent.get("accounted_bytes") or 0)
    if accounted <= 0:
        raise PublishConflict("intent accounted_bytes 非法（%r）" % accounted,
                              task=task)

    # 锁内重验（步骤 2）+ 收口（步骤 4）：两段短事务各自先取同一 advisory
    # 锁；状态机 CAS（任务 committing/资产 staging）保证两段之间无 publish/
    # delete/cancel 能插入（cancel 拒绝 committing；delete 只对 ready CAS）。
    # P4-b 重审（P3 偏差 #1 义务）：FS 发布先于本锁的顺序对 COS worker
    # lease 模型同样成立——validating+intent 是不可撤销提交段（cancel 被
    # CommitInProgress 拒），旧 generation 被 worker fencing 拒绝结算，
    # no-clobber+verify_bundle 幂等兜底重复发布，可见性由 settle 事务的
    # asset_state CAS 唯一裁定（详见 ingestion_store.IngestionPublishChannel）。
    conn = _connect()
    try:
        with pg_store.transaction(conn):
            with conn.cursor() as cur:
                slide_store.acquire_slide_lock(cur, slide_id)  # 第一把锁
                channel.precheck_locked(cur, task_ref, generation, slide_id,
                                        owner_user_id, intent)
                cur.execute("SELECT asset_state FROM slides WHERE slide_id=%s",
                            (slide_id,))
                srow = cur.fetchone()
                # 锁内资产状态重验：走到这里（任务 committing+intent 未清）
                # 资产必为 staging——settle 的 CAS 才把它推向 ready；目标包
                # 是否已发布（bundle_already）不影响该前提。
                if srow is None or srow["asset_state"] != \
                        slide_store.SlideState.STAGING:
                    raise PublishError(
                        "asset_state_conflict",
                        "锁内资产状态漂移（%r != staging）" %
                        (srow and srow["asset_state"],),
                        deterministic=True, task=task)
    finally:
        conn.close()

    return channel.settle(task_ref, generation, slide_id, sha256, accounted)


def publish_slide(task_ref, generation, slide_id, manifest=None, *,
                  owner_user_id=None, upload_root=None):
    """统一发布入口（upload_tasks 默认通道；V2 原生单文件 + V1 单文件）。

    P4-b：任务族无关的六步编排在 ``publish_with_channel``；本函数 = 默认
    ``UploadTaskPublishChannel`` 的便捷包装（既有 V2/V1 调用方零改动）。
    参数与返回值语义见 ``publish_with_channel``。
    """
    return publish_with_channel(
        task_ref, generation, slide_id, UPLOAD_TASK_CHANNEL, manifest,
        owner_user_id=owner_user_id, upload_root=upload_root)


def read_intent(task):
    """任务快照 → (generation, intent) | (None, None)；恢复扫描入口用。

    损坏 intent（{}）也返回（调用方 fail-closed 保持 committing 告警）。
    """
    intent = _decode_intent(task)
    if intent is None:
        return None, None
    gen = intent.get("generation")
    return (str(gen) if gen is not None else None), intent


def staging_absent(task_ref, generation, *, upload_root=None) -> bool:
    """发布源 staging 目录不存在（恢复路径判定「全未发布」的证据之一）。"""
    return not slide_storage.staging_dir(
        task_ref, generation, root=upload_root).exists()
