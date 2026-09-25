# P3 设计合同：统一本地发布（V2 + 原生单文件 V1）

日期：2026-09-26。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §3 与 [P1 合同](slide-id-refactor-p1-contract-20260925.md)。
前置：P1（门禁/存储/回填）+ P2 后端（关系/API/响应 slide_id）+ P2 前端（按 ID 操作）已就位。**P3 是第一个产生 id_bundle 资产的阶段**——写完本阶段，新上传即新 ID + 独占目录；legacy writer 只剩 V1 ZIP（P4）。

## 1. 范围与顺序

- **接入**：V2 分片上传、V1 原生单文件上传（`_needs_conversion` 为假的可直接打开格式——SVS/TIFF/NDPI 等白名单以 `slide_io` 试开为准）。
- **不接入**（保持 legacy 路径不动）：V1 ZIP/MRXS、KFB 转换链、COS、百度导入、CLI 导入（全部 P4）。
- **同时收口**（P1-B2/P2 遗留义务，本阶段必须消化）：
  1. 机器通道（internal/plugin）与本地免认证态的「无 slides 行即可读」兼容分支 → 删除，一律拒（P1-B2 偏差 #2/#3）；受影响既有测试夹具按"先注册行"调整（允许调夹具，不删断言）。
  2. `annotations_by_slide` 分组键切 slide_id（P2 偏差 #1 门禁项：id_bundle 资产可同 original_filename，按键名分组会串）。消费方同步切。
  3. 上传响应的 slide_id 改从任务绑定取（P2 补丁按名 resolve 的 `get_slide_id(safe)` 对 legacy_filename=NULL 的新资产返回 None——V1/V2 commit/status/zip/conversion/COS 响应改读任务行的 slide_id 列；转换/COS 在 P4 才出 ID，本阶段保持按名解析并在注释标注）。

## 2. 迁移 0068（`migrations/0068_upload_publish_intent.sql`）

- `upload_tasks ADD COLUMN commit_intent_json TEXT`（可空）——持久化 publish intent（task_ref、generation、slide_id、owner、manifest、sha256、accounted_bytes；计划 §3.1-3："不能仅保存目标文件名"）。
- 惯例同 0067：幂等、单事务、注释镜像合同；`tests/test_pg_infra.py` 清单 +0068（编排方补或代理补——谁做都要过 test_pg_infra）。

## 3. 写通道流程（V2 / V1 单文件统一）

### 3.1 创建（V2 `api_uploads_create` / V1 `api_upload` 入口）
1. 身份 → owner（`current_identity`；无 UID 的本地模式解析配置 owner——沿用现有 `_OWNER_USER_ID` 口径，**不允许空 owner 自动认领**）。
2. `slide_store.allocate_slide(owner, original_filename=客户端原名, format_ext=白名单后缀)` → staging/id_bundle 资产行 + slide_id。**同一事务**写 `upload_tasks.slide_id`（V2 create_task 加形参；V1 在 begin_legacy_commit 时绑定）。幂等重试复用原任务及其 slide_id（V2 现有幂等语义不变；upload_tasks.slide_id 是唯一绑定源）。
3. 预约（reserve_upload）语义不变。
4. 响应立即可带 slide_id（创建即绑定）。
5. **删除 `_upload_name_conflict` 在 V1 单文件/V2 的调用**（app.py:11148/11791）：同名不再冲突；no-clobber 由 objects/<slide_id> 唯一性兜底。`allow_kfb_recover`（同名同 SHA 认领）随 V1 单文件切换一并拆除其调用（转换链的替代语义在 P4 处理——本阶段 KFB 仍走 legacy V1 分支吗？**裁决：`_needs_conversion` 为真的 V1 上传本阶段继续走 legacy 路径（含旧冲突检查），只把原生可开格式切新管线**——转换链 P4 整体改造）。

### 3.2 暂存
- V2 part 文件、V1 `.uploading-*` 全部改落 `slide_storage.staging_dir(task_id, generation)`（`.staging/<task_id>/<generation>/`）；generation 语义：V2 用任务的 commit 代次（begin_commit 递增或 commit_token 派生——选一个稳定的、崩溃恢复可重判的代次来源并写入 intent）；V1 单请求 generation=1。
- 旧平铺暂存名（`.uploading-*`/`.part-*`）对新任务不再产生；存量在途任务（升级窗口）由旧恢复逻辑排空（P6 处理新旧并存）。

### 3.3 提交/发布（slide_publish.publish_slide 真实接线）
按 slide_publish.py 骨架合同逐步：
1. 前置验证：任务存在、归属 owner、slide_id=任务绑定、资产 staging、generation 匹配。
2. 复算 SHA/大小/格式试开（在 staging 文件上，沿用 `_validate_slide_file`）。
3. 持久化 intent 到 `upload_tasks.commit_intent_json`（同事务把任务置 committing——复用 begin_commit 的 CAS）。
4. `slide_store.acquire_slide_lock(slide_id)`（advisory 第一把锁）→ 锁内重验（generation/staging/未取消/预约有效）。
5. `slide_storage.publish_bundle_no_clobber(staging, slide_id, manifest)`（manifest entry=`data.<ext>`，files 含 size/sha256）。
6. 短事务：`slide_store.mark_ready(staging→ready, accounted_bytes=实际字节)` + `upload_task_store.finish_commit`（consume reservation）+ 清 commit_intent。**同事务**；任一步失败整体回滚（FS 已发布、DB 未提交 → 不可见，恢复重试收口）。
7. 崩溃恢复（`_upload_v2_maintain`/`_upload_legacy_recover_commit` 的对应路径）：committing 且有 intent → 幂等重跑 5-6（目标已存在且 manifest/sha 吻合 → 跳过 FS 只做 DB CAS；不吻合 → fail-closed 告警，不删不猜）；committing 无 intent → 回滚 active 或 failed（语义同现状）。
8. 取消/失败：清理 staging 目录后释放 reservation（计划 §3.3：清理确认后释放）；staging 资产行 → failed（保留证据）。

### 3.4 项目自动关联
V1/V2 的 target_pid 关联（`add_slides_to_project`）改传 slide_ids（P2 已支持双列）——关联正确 ID，不因同名关联其他资产。

## 4. 读侧对新布局的验证

- `_visible_slide_names`/`api_slides`：确认 id_bundle 行（legacy_filename=NULL）正确出现在列表（`name` 字段为 None，前端按 slide_id+display_name 消费；旧字段兼容——若旧前端/旧测试按 name 非空假设崩溃，在 DTO 层给 name 一个确定性替代串？**裁决：name 输出 None，旧客户端对新资产的兼容不保证（计划 §4.1：新资产只能由支持 ID 的客户端操作）**；`_slide_info_dict_desc` 已按 descriptor 出数，核对 name=None 序列化路径）。
- `_visible_slide_names` 返回集合对 id_bundle 行改为 `{slide_id}` 集合语义——检查其全部消费方（分享校验/项目校验等）按 ID 分支走（P2 已双字段，此处核对无按名漏洞）。
- 读端点（info/dzi/tile/...）对 id_bundle 资产经 descriptor 路径读 objects/<slide_id>/data.<ext>；tile 缓存键/render token 已是 slide_id（P2 R-15）。
- `_legacy_slide_revision`：id_bundle 资产改从 `slide_assets` 记录/发布 manifest 取 revision（不再是 mtime:size——计划 §2.1 "slide_assets revision 校验保留"）；**裁决落地：发布时 `record_slide_asset(slide_id, legacy_revision="<sha256 前缀或 manifest 摘要>")` 写一行，读侧 descriptor.revision 已有（P1-A 的 _DESCRIPTOR_SQL 取 slide_assets 最新行）**——mtime:size 只在 legacy 布局保留。

## 5. id_bundle 资产的删除（本阶段最小正确版，P5 再统一两阶段）

- 新端点 `DELETE /api/slides/<slide_id>`：advisory 锁 → `request_delete`（ready→deleting，立即拒读）→ 授权联动清理（view grants 按 slide_id 删、demo 撤销、run grants 撤销——复用现有按键清理函数，注意它们现在按名/按 ID 两态）→ `slide_storage.remove_bundle` → 同一控制流里 `mark_deleted` + used_bytes 幂等减 accounted_bytes（upload_guard 新增/复用结算函数；R-12：只对新 ID 资产减账，legacy 资产维持不回退）→ 审计事件。
- 旧 `DELETE /api/slide/<name>`：legacy 布局资产维持旧路径；若 name 解析到 id_bundle 资产（不可能——新资产无 legacy_filename），无需分支。
- 同名重传：新上传恒新 ID（allocate_slide 不查原名）；`set_slide_meta` 的 deleted→ready 复活分支**本阶段保留**（ZIP 仍走 legacy），P4 随 ZIP 切换一并拆除并重写 `test_slide_delete_clears_view_grants_no_orphans` 为目标不变量（重传=新 ID、旧分享/授权/标注不继承）。

## 6. 测试要求（计划 §8 矩阵的 P3 部分）

新测试文件 `tests/test_slide_publish_v2_pg.py`（或并入既有上传测试文件，按内聚选）：
1. V2 同名并发（两账户同名同字节）→ 不同 slide_id/不同 objects 目录、各自归属、互不冲突 409。
2. 同一任务重复 commit/响应丢失重试 → 同一 slide_id、一次配额结算、无重复资产。
3. intent 前后/包发布前后/DB commit 前后崩溃（用 monkeypatch 阶段屏障注入，不用 sleep）→ 未 ready 不可读；恢复收口一次；存活字节有预约或实占。
4. 取消与发布并发、删除与发布并发 → 有明确胜者；新 ID 资产不受旧任务清理影响。
5. 满配额、预约过期 → 不可读、不漏账、不重复收费。
6. 删除 A（id_bundle）→ 同名重传 B → 新 ID；旧分享/授权/标注/AI run grant/Demo 不指向 B（衔接 P2 关系测试）。
7. 删除减账幂等：重复 DELETE/worker 重试不重复减。
8. 显示名修改不移动文件、不动 revision。
9. 旧端点对 id_bundle 资产经 alias？——新资产无 alias，旧端点按名找不到（404）是**预期**（计划 §4.1）；列表/info/dzi/tile/crop/region 经 ID 端点全通。
10. 机器通道无行兼容分支已删：无行文件经 internal/plugin 通道 404/403。
11. annotations_by_slide 对同名 id_bundle 资产按 ID 分组不串。

既有全量门禁（清 /tmp 残渣后）：`pytest -q --ignore=tests/test_e2e_pg_reap.py --ignore=tests/e2e` 仅允许已知无关失败（admin 0.4.13）；`npm run test:js` 全绿（前端 P2 已按 slide_id 消费，上传响应字段本阶段从任务绑定取值——注意检查 tests/js 相关 mock）。

## 7. 部署形态注意

- Containerfile 的模块清单若无变化则不动；新迁移 0068 随 ensure_schema 自动应用。
- 旧版镜像回滚边界：0067+0068 均为增列不删列，旧代码读到新列不受影响（旧代码不查 asset_state）——但**新资产（id_bundle）对旧版镜像不可见**（旧代码按目录扫描+legacy 路径读不到 objects/），计划 §7.2 的回滚纪律已覆盖（一旦产生新 ID 资产不回滚到只认文件名的老镜像）。本阶段实现时确保 staging 目录命名不把旧版 `_visible_slide_names` 搞崩（旧版只看根目录 regular file——`.staging/`/`objects/` 是目录，会被旧版当伴侣目录跳过还是报错？在测试里验证旧式扫描函数对这两个目录的行为并记录——只是评估，不回改旧代码）。
