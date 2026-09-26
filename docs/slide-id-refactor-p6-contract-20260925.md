# P6 设计合同：迁移演练工具链 + 运行时 legacy 读取路径退役

日期：2026-09-26。配套[实施任务书](slide-id-storage-refactor-agent-plan-20260925.md) §3.3/P6 与[迁移手册](slide-storage-migration-audit-runbook-20260925.md) §3/§4/§5/§6（手册的算法/崩溃恢复/硬门禁口径是本文的强制输入，冲突以手册为准）。
前置：P0 审计工具（`scripts/audit_slide_identity.py`）+ P1-B1 回填（`scripts/backfill_slide_asset_state.py`）+ P5（统一删除/账本）。

**范围边界**：全部演练在脱敏副本/合成数据上执行；**不操作生产**、不部署、不打开 COS capability。生产切换方案属 P7。

## 1. 三个工具（手册 §3 表格的职责不可缺）

### 1.1 `scripts/plan_slide_migration.py`（纯计划，零副作用）

- 输入：冻结审计产物（`inventory.jsonl`/`issues.jsonl`，P0 工具输出）+ 目标环境标识（`--env`，如 `drill-local`）。
- 输出 `migration-plan.jsonl`：逐资产一项——保留的 slide_id、冻结旧 alias（legacy_filename）、owner 及证据来源、源相对路径/大小/sha256、目标 `objects/<slide_id>/` 布局与 entry、格式、动作（`migrate`/`quarantine`/`retain_history`）、授权映射摘要（share/grant/项目计数，不含秘密）、回滚定位（源路径保留位）。
- 确定性：同输入同输出（排序稳定、无随机/时间戳参与 ID 或计划内容；计划头带 version/env/输入摘要 sha256）。**绝不重新分配已有 slide_id**（冻结裁决）。
- 动作口径（手册 §7.1）：文件+元数据一致且 owner 明确 → migrate；文件缺失 → retain_history（failed+reason，不迁授权）；文件在元数据不在/owner 矛盾 → quarantine（隔离报告，不猜不绑）。

### 1.2 `scripts/migrate_slide_storage.py`（默认 dry-run）

- `--apply` 三件套缺一不可：`--plan <file>` + `--plan-digest <sha256>`（与计划头一致才执行）+ `--env`（与计划头一致）+ `--quiesce-proof <text>`（停写证据字符串，记录进 journal——演练环境同样强制，防把测试计划打到生产）。
- 逐项状态机：`planned → copied → verified → bound → postverified`（持久 journal：`migration-journal.jsonl` + 可选 DB 表；崩溃后重跑从 journal+manifest 共同续判，**不以单文件存在为成功依据**）。
- copied：目标卷私有 staging（`.staging/migrate-<plan>/<slide_id>/`）复制全包（MRXS 伴侣/associated 全成员）；**禁止与源共享 inode**（复制不硬链接）；空间不足阻塞，不降级删源。
- verified：逐文件 sha256+大小全对 + `slide_io` 代表性试开（格式真实可读）+ fsync。
- bound：`slide_storage.publish_bundle_no_clobber` 入 `objects/<slide_id>/` + 短事务 CAS（slides 行 storage_layout→id_bundle、storage_relpath→新位、accounted_bytes 校准；advisory 锁序同主方案）。**不重复计上传配额**（used_bytes 不动——历史资产已在账本内或无账本责任，R-12 过渡口径不变）；授权映射零变更。
- postverified：独立重读 DB+磁盘确认绑定与引用。
- **不删源**：旧平铺文件保留原位（只读备份语义，清理属上线计划的独立可审查 manifest，手册 §4）。
- 已存在目标：仅当 journal+manifest 证明同一迁移项才幂等复用；否则报冲突中止（不依内容相同认领）。

### 1.3 `scripts/verify_slide_migration.py`（独立核验，失败非零）

- 不读迁移日志的 success 字段——独立重读实际 DB/磁盘：每 ready 资产包完整性（manifest 逐文件 sha/大小）、descriptor 路径可解析可读、授权/分享/项目/标注引用逐条落点、同名 tombstone 不复活、配额账本对账（每 owner used 与资产 accounted 合计的关系报告——含「不等于」的合法原因披露）、无意外新增可见性（授权差异同时报意外缩小）。
- 输出 `verification.json` + 人读 `summary.md`（脱敏计数、incomplete 项、go/no-go 建议）。任何扫描错误/权限失败/读取变化 → incomplete，**不得报告全量通过**。

### 1.4 复用纪律

三工具只允许调用 `slide_store`/`slide_storage`/`slide_io`/`pg_store`/`share_store` 的既有原语与只读查询，**禁止**再实现一套路径拼接/owner 推断/状态迁移（手册 §3 首行）。缺失原语先补进模块再加测试。

## 2. 演练（drill）要求

合成/脱敏副本环境（pgserver + 临时 UPLOAD_DIR，`tests/` 或 `scripts/drill_*` 夹具）覆盖：

1. 全受支持格式代表样本（svs/tif 单文件、mrxs+伴侣目录、kfb→转换产物、kfbf→ome）。
2. 历史关系全谱：分享（share_slides 成员）、显式授权、项目成员、标注、demo 目录、run grants。
3. 中断恢复：copied 后杀进程重跑不重复复制/绑定/计账；bound 后杀进程重跑只补 postverify。
4. 隔离类：quarantine/retain_history 项不进用户列表、不可读、报告准确披露。
5. 同 legacy 名 tombstone + 新同名资产并存：迁移不碰 tombstone，旧分享/授权不继承新内容。
6. 演练证据（计划摘要、journal、verification.json、summary.md）落 `docs/drill-evidence-<date>/` 或测试断言内（二进制大文件不入仓）。

## 3. 运行时 legacy 读取路径退役（演练绿了之后做，单独提交点）

- `slide_storage.resolve_descriptor_path`/读端点的 legacy 平铺分支（`UPLOAD_DIR/<legacy_filename>` 直接命中）从**运行时正常链路**移除——legacy_filename 列保留为冻结 alias（固定 ID 查找/书签），不再承载物理读取旁路。迁移工具的读取在受限脚本内保留（标记「迁移专用」注释）。
- 随附拆除（P4 遗留清单）：
  - `conversion_store.canonical_is_live` 兼容壳 + `NameConflict` 类 + baidu_ingest 的两个调用点（baidu convert 分支收口）；
  - 升级窗口在途旧任务分支：`_upload_legacy_promote_state` 三态恢复（app.py:11811–11859 一带的平铺补归属路径）与 `_upload_v2_set_ownership` 旧 V2 窗口分支（:13082 一带）——排空语义随运行时退役移除（在途旧任务恢复=升级窗口义务，最终版本不背）；
  - `_canonical_name_for` 若仅剩展示快照用途则保留并注明，其余用途清零核对；
  - `.uploading-*.lock` sidecar 平铺收进任务 staging 目录（P3 裁决收口）。
- `share_server` 与 HP 侧若仍有 legacy 平铺物理读取残留（P2/P3 已切 ID 的地方复核）一并收口。
- `shares.slides` JSONB 快照列**不在本阶段 drop**（手册 §6：调用者迁完且审计通过后独立 contract migration）——P7 文档列出退役顺序即可。

## 4. 测试

- 新增 `tests/test_slide_migration_tools.py`：计划确定性（同输入同摘要）、三件套缺任一拒绝 apply、逐状态机推进/中断重跑幂等、no-clobber 冲突中止、quarantine/retain_history 口径、verify 独立性（篡改 journal 的 success 仍被独立核验抓出）、不计上传配额、授权零变更。
- 运行时退役：legacy 平铺文件不再可读（404/403）、冻结 alias 的固定 ID 查找仍工作（分享 token 按 ID 成员照常）、升级窗口分支删除后既有新链路回归全绿。
- 演练作为测试或脚本化证据（§2 六项）。

## 5. 门禁

全量 pytest（先清 /tmp 残渣 + TMPDIR 重定向）仅允许已知无关失败（admin 0.4.13）；`npm run test:js` 全绿；HP contract 复跑全绿（legacy 物理读取退役对 HP 旧会话映射的失效语义需跨仓核对——旧会话指向已迁移资产按 ID 照常，指向 tombstone 明确失效）。

## 6. 偏差与报告

同 P4：逐条披露偏差（什么/为什么/替代方案）；交付报告含交付清单、拆除证据（rg 前后对照）、演练证据索引、门禁输出、遗留（P7 输入）。
