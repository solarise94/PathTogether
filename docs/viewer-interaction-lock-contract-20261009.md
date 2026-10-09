# 标注几何位置锁：后端契约（§6.3 / §6.4 实施记录）

日期：2026-10-09。分支 `vi-lock`（基线 5328fefc）。实施依据
`docs/viewer-interaction-skills-review-design-20261009.md` §6.3/§6.4 步骤 D 后端部分。
本文是**前后端并行开发的契约文档**：前端按下述请求/响应/错误码实现；与任务书
契约的偏离在 §7 显式列出。

## 1. 存储模型

- `rois.data`（JSONB）内顶层 `geometry_locked: boolean`，缺省/缺失 = `false`。
- **无新表、无迁移**（不占用 0082）。revision / history / change_seq 全部沿用现有机制。
- 新写入行恒落显式布尔（`add_roi(geometry_locked=...)`）；读侧对旧行缺键归一为
  `false`（`_roi_out` / `public_roi_view` / `annotations_by_slide` 等输出路径）。

## 2. 读取接口（全部条目含顶层 `geometry_locked`）

`/api/annotations`（单切片/项目/默认聚合）、`/api/slides/<slide_id>/annotations`、
`/api/annotations/changes`、`/api/slides/<slide_id>/changes`、`/api/share/rois`、
history（`/api/annotation/<token>/<index>/history`）、`POST /api/annotation` 响应、
PATCH 响应、restore 响应、分享页 `/s/<token>/api/rois` 及其 PATCH 响应。

- 投影经 `annotation_access._PUBLIC_ROI_KEYS` 白名单（已加 `geometry_locked`），
  分享投影按既有可见性规则（`public_roi_view`，缺键补 `false`）。
- tombstone 最小字段输出不含锁状态（沿用现有最小形状）。

## 3. PATCH 语义（锁切换）

适用于：

- `PATCH /api/annotation/id/<annotation_id>`（工作台，按 ID）
- `PATCH /api/annotation/<token>/<index>`（工作台策展/编辑，token 变体）
- `PATCH /s/<token>/api/roi/<index>`（分享页，见 §7 偏离 1）

字段皆可选：现有几何字段（`geom`）、现有备注/元数据字段（`note`、`shared`、
grants 等）、`geometry_locked: bool`、`expected_revision: int`。

处理顺序（与任务书一致）：

1. **读/写权限验证先行**（`_resolve_anno_by_id` / `_check_annotation_owner` /
   分享端 `_roi_owned_by`）。权限失败按既有错误返回（403/404），不泄露锁状态。
2. `geometry_locked` 非布尔 → `400 {"error": "geometry_locked 需为布尔值"}`。
3. 同一请求带 `geometry_locked` 且带几何字段（`geom` 非 null）→
   `400 {"error": "lock_with_geometry"}`。
4. 带 `geometry_locked` 但缺 `expected_revision` →
   `400 {"error": "expected_revision_required"}`（仅锁切换；旧客户端其它 PATCH 不受影响）。
5. store 层在**同一事务、行锁（SELECT … FOR UPDATE）内**检查（`_check_geometry_lock`，
   先于 CAS）：
   - 当前已锁且请求改几何 → `409 {"error": "annotation_locked", "current": <条目>}`；
     `current` 为 `public_roi_view` 投影（含 `revision`/`geometry_locked`/几何）。
   - revision 不符 → `409 {"error": "revision_conflict", "current_revision": N,
     "current": <条目>}`（by-id 路由补 `current`；token/分享路由沿用旧形状，
     见 §7 偏离 3）。
   - 两者同时成立 → **annotation_locked 优先**。
6. 已锁时只改备注/元数据（note/shared/grants/review）→ 200，revision 递增，
   history 记录（快照含 `geometry_locked`）。
7. 锁切换本身：revision 递增；history 记 `op: "lock"|"unlock"` + `actor`
   （工作台 `user:<uid>`；分享页 `"visitor"`，不落访客哈希）；change_log 写
   op=`lock`/`unlock`，change_seq 变化流可见。
8. **同请求锁+备注**：允许。锁切换先行 bump revision，随后的 note 更新用链式
   新 revision 继续 CAS（`expected_revision` 对请求时点的初始 revision 负责）。
   锁+shared：shared 先行（不 bump revision），锁随后。

任何有编辑权的人（创建者 / annotation_grants can_edit / owner 角色×可写切片 /
分享页创建者访客）可锁/解锁；只读授权、非创建访客、无写权限查看者 → 403。

## 4. DELETE 语义

`DELETE /api/annotation/id/<id>`、`/api/annotation/admin/<index>`、
`/api/annotation/<token>/<index>`、分享页 `DELETE /s/<token>/api/roi/<index>`：
已锁 → `409 {"error": "annotation_locked", "current": <条目>}`（行锁内判定，
先于 CAS）。解锁后可删。切片删除（统一删除编排）、账号删除等生命周期清理
不经 `delete_roi*`，不受锁阻挡（有测试断言 `DELETE /api/slide/<name>` 200）。

## 5. 创建与 AI 路径

- `POST /api/annotation` 与分享页 `POST /s/<token>/api/roi` 接受可选
  `geometry_locked` 布尔（草稿保存时带锁）；创建时**允许**与几何字段同给。
  非布尔 → 400。POST 响应带 `geometry_locked`。
- AI 写路径（`/internal/ai/annotate`、`/api/plugin/v1/.../annotations`）不传该参
  → 恒 `false`；授权逻辑未改（另一线负责）。

## 6. history / changes 事件形状

- history 快照（`_append_history`）：既有 `geom/note/label/revision/ts` 之外新增
  `geometry_locked`（修改前状态）；锁切换快照额外带 `op: lock|unlock` 与
  `actor`。上限 20 条不变。
- `/api/annotations/changes` 的事件是**状态式**（roi 当前快照 + 最新 change_seq，
  与既有 update/delete 语义一致，不新增 op 字段）：消费方在锁切换后会读到携带
  新 `geometry_locked`/`revision` 的条目。前端不得假设「每次操作一条事件」。

## 7. 与任务书契约的偏离 / 补充说明

1. **`share_server.py` 被修改**（任务书允许清单未列）。原因：(a) store 层锁检查
   以异常（`share_store.AnnotationLocked`）在同一事务抛出，分享页 PATCH/DELETE
   调用同一 store 函数，不接住会 500；(b) 任务书「分享 `PATCH /api/annotation/
   <token>/<index>`」按 app.py 既有同名路由实现，但分享页（`/s/<token>/api/roi/...`）
   也需要同一锁语义（草稿带锁、访客锁/解锁、只读访客拒绝）。改动最小：错误映射
   + 同 §3 的字段校验 + POST 接受 `geometry_locked`。
2. 错误码 `expected_revision_required` / `lock_with_geometry` 按任务书；
   `geometry_locked` 非布尔的错误码未在任务书指定，采用项目中文消息风格
   `geometry_locked 需为布尔值`（与 `public 需为布尔值` 同款）。
3. `revision_conflict` 响应：by-id PATCH/DELETE 补了 `current` 投影；token
   PATCH、分享路由、admin DELETE 沿用旧形状（`current_revision` only），
   避免无谓行为面扩大。前端如需 current 可改用 by-id 路由。
4. restore（`POST /api/annotation/id/<id>/restore`）**不做锁检查**：已锁标注的
   API 删除本来就被拒（409），tombstone 上不存在「后加的锁」可被恢复绕过；
   restore 仅做既有 CAS。响应新增 `geometry_locked`。
5. 变化流为状态式事件（见 §6），非逐操作日志——与既有语义一致，未为锁新增
   事件类型。
6. store 新增公共名：`share_store.AnnotationLocked`（异常，携带 `current`）、
   `share_store.set_roi_geometry_locked(annotation_id, geometry_locked,
   expected_revision=None, actor_user_id=None, actor="")`。

## 8. 验证

- 新测试：`tests/test_annotation_geometry_lock.py`（16 用例，覆盖任务书清单全项）。
  基线证明：`git stash` 实现改动后运行 → **15 failed, 1 passed**（唯一基线通过
  用例是「权限失败先于锁检查」守卫，基线上本就该通过）。
- 子集：`TMPDIR=/home/solarise/pytest-tmp .venv/bin/python -m pytest tests -k
  "annotation or share or roi" -q -p no:cacheprovider` → 171 passed, 2 skipped。
- 附加：`tests/test_collaboration.py tests/test_ai_drawing_gate.py
  tests/test_plugin_api.py` → 95 passed。
