# 2026-09-20 未提交改动 review

结论：暂不建议上线。发现 8 项需要修复的问题；不只是缺浏览器验收。此次未修改产品实现、未提交、未安装插件或操作线上库。

审查范围：PathTogether 与 HistoPilot 当前未提交 diff 和新增文件，重点为隔离、授权、撤销、附件及概览请求组装。

## R1 [P1] AI 读取没有绑定当前主体，会串入他人的私有 AI 标注

位置：`annotation_access.py:226–234`；`app.py:_internal_ai_read_subject`；`HistoPilot/src/flask-client.ts:304`、`src/platform/http-client.ts:473`。

无属主 AI 分支返回 `is_ai_generated(roi)`，只验证有 AI 来源/会话溯源，没有验证记录是否属于当前用户或当前会话。这不是 fail-closed：用户 B 的 `shared=false`、`created_by_session_id=session-B` 的 AI 标注会被放行。

已核实两个实际客户端的 spots/changes 都只发送 slide 和 cursor，没有 session_id；runner 的 findSpot/injectSpotChanges 也使用这条链路。因此该回退是当前正常路径，不只是旧部署风险。还会使普通人工 marker 在 findSpot 时不可见，导致从自己的人工标注发起分析报告“该标注已删除”。

修复：完整贯通经验证的读取主体/会话绑定；未绑定时返回空集合或拒绝，不按 source=ai 跨用户放行。lite fork 不发写 grant，需要独立合法读取身份，不能依赖写 grant 推导一切读取。覆盖 main/fork/branch、自己人工标注、B 的私有 AI 标注及只读模式的端到端测试。

验证：纯策略探针，B 的私有 AI 标注对 user-A 返回 False，对无绑定 AI 主体返回 True。

## R2 [P1] 显式授权一条标注会泄漏其原分享链接 token

位置：`share_store_pg.py:2043–2046`；`share_server.py:1695–1704`（`_public_roi` 只删除 visitor）。

把来自 S1 的一条标注授权给用户 B 或分享 S2，响应仍带记录原本的 `token=S1`。这是可以访问 `/s/S1` 的 bearer token，不是无害的标注 ID；接收者可据此访问 S1 的其他切片和链接公开内容，超出“只分享这一条”的授权范围。

修复：普通标注、changes、评论和分享响应采用白名单投影，不返回来源分享 token；跨主体操作以 annotation_id 定位并重查权限。不能仅修改某个页面隐藏 token。

验证：独立临时 PostgreSQL 中，创建 S1 标注 → 授权 user-B → `annotations_by_slide(subject=B)` 返回项包含完整 S1 token。

## R3 [P1] 授权变化没有进入增量流，既有会话不会失效

位置：`share_store_pg.py:1333–1365` 的授权/撤销事务；`app.py:api_annotation_set_shared`。

grant/revoke 只更新授权表和 visibility_status，没有 bump change_seq 或通知订阅者。已推进到当前 cursor 的用户收到新授权后取不到新标注；撤销后过滤只会让记录消失于新列表，不会产生失效通知。既有 AI transcript/checkpoint 中已注入的标注仍可能继续进入模型请求；撤销路由也没有清理这些上下文。

修复：引入按受影响主体投递的权限变更/重置事件；新增授权触发可见集合重建，撤销触发浏览器与 AI 上下文失效并在继续/回放时校验。仅为原 ROI bump seq 不足以通知被撤销者，因为读取过滤会再次吞掉该事件。

验证：独立临时 PostgreSQL 中，grant、revoke 前后 current_change_seq 不变；从操作前 cursor 获取 changes 均为零条。

## R4 [P1] 创建→撤销→重做必然撞幂等键唯一索引

位置：`static/app.js:4685–4693`；`migrations/0056_annotation_visibility.sql:64–66`；`share_store_pg.py:add_roi`。

重做复用原 client_action_id，删除仅设 tombstone。唯一索引涵盖所有行，而 add_roi 的幂等查询和冲突后回读只查 NOT deleted，所以插入失败后也找不到可返回的行，最终抛 UniqueViolation。

修复：明确重做契约：受权限及 revision 保护地恢复原 annotation_id，或为新的创建动作生成新的幂等键并维护撤销历史引用。不能简单放宽唯一索引，使网络重试把用户已经撤销的标注重新创建。

验证：独立临时 PostgreSQL 应用当前全部迁移，调用真实 add_roi → delete_roi → 同键 add_roi，复现 `UniqueViolation: idx_rois_owner_client_action`。

## R5 [P1] 撤销使用最新 revision，反而会覆盖协作者的新修改

位置：`static/app.js:4624–4633`；创建撤销和编辑重做也使用类似逻辑。

例如 A 编辑到 revision 2，B 后续编辑到 revision 3，A 列表已刷新。A 撤销自己的操作时读取最新 item.revision=3，以该版本为 CAS 预期写回旧 before；后端认为合法，B 的修改被覆盖。现有 CAS 只保护“读取列表之后”的竞争，没有保护“自己那次操作之后”的协作修改。

修复：记录本次提交返回的 revision，并将它作为撤销前置条件；成功撤销/重做后更新历史版本链。遇到协作者后续修改应冲突或明确合并，不自动采用最新 revision。后端 PATCH 响应应返回权威 revision。

验证：执行真实 undoEditEntry 函数的最小 Node 探针；历史 revision=2、当前 item revision=3 时，实际 PATCH 使用 expected_revision=3。

## R6 [P1] 撤销目标消失后回退旧 index，可能删除另一条标注

位置：`static/app.js:4587–4599`；`share_store_pg.py:delete_roi`。

undoCreateEntry 按 annotation_id 找不到目标时仍使用 entry.index。token 下索引按未删除行重排：同账号创建 A(index=0)、B(index=1)，另一窗口删除 A 后 B 变成 index=0；此时撤销 A 会用旧 index=0 删除 B。两条标注都为 revision 1 时 CAS 不会阻止。即使本地查到 ID，查找与请求之间发生删除仍有竞争窗口。

修复：撤销/重做及其他写入必须由服务端按稳定 annotation_id 定位。兼容 index 路径至少在同一锁/事务中同时校验 expected_annotation_id；目标不存在应视为已撤销，不能回退到可能指向别人的位置。

验证：静态核对前端回退分支与后端 `_fetch_live_rois_locked` 的非 tombstone 排序语义；此项未做双窗口浏览器复现。

## R7 [P1] 附件卡片显示已加入，但发送丢失实际附件语义

位置：`HistoPilot/integrations/pathtogether/ui/main.js:1610–1617`、`freezeAiRunBodyAndAttempt`；PathTogether 的 AI 网关转发白名单。

当前多张卡片只发送最后一张的 bbox，却在成功后消费全部卡片；marker 的 annotation_id/revision/geometry/note 均未发送，因此不能校验标注版本/撤权，也不是用户选择的“加入当前 marker”。同时保存的 render_context 没有写入请求，freezeAiRunBodyAndAttempt 会补发送时的实时通道状态：加入后切通道，模型看到的是另一种图像内容。

修复：建立完整附件数组契约，贯通宿主→插件→网关→runner→模型请求；每项冻结 bbox/render context，并对 marker ID/revision 做服务端授权校验。只消费真正接受的附件。若短期只允许单个视野，应明确限制 UI，不能展示并清空实际没有发送的多项附件。

验证：执行实际 applyPendingAttachesToBody 函数的 Node 探针；输入 marker+viewport 两张卡，输出只有最后 viewport，返回消费列表却包含两项；无 marker ID 或冻结 render_context。

## R8 [P1] 原“公开标注”按钮未接入新授权模型，正常分享流程失效

位置：`static/app.js:5056–5077`；`app.py:api_annotation_set_shared`、`share_store_pg.py:set_roi_shared`。

界面仍只提交 `{shared:true}` 并点亮公开图标，而新后端对 token=admin 只改标志，不创建任何 user/share grant。当前前端没有 grantee_kind/grantee_id 的选择和提交路径。用户按现有界面“公开标注→分享切片”完成操作后，接收者仍看不到标注；只有手工调 API 才能显式授权。

修复：在标注/分享界面提供目标用户或分享链接及标注集合选择，提交实际 grant，显示授权结果；旧“公开”按钮改名并绑定明确目标。迁移收紧可见性可以保留，但需要可用的正常协作入口。

验证：静态检查按钮 payload、后端新语义和前端 grant 调用缺失。应补“创建人工标注→选择目标分享→接收者看到→撤销后失效”的浏览器用例。

## 本次验证与边界

- HistoPilot 定向复跑：`npx vitest run test/fork-overview.test.ts test/request-assembler.test.ts test/ui-attach-draft.test.ts`，3 文件、62 项通过。
- 对 request-assembler 的 overview-only checkpoint 调整，本次定向 review/测试未发现独立阻塞；这不覆盖平台身份链路，不能据此认定 marker 分析端到端可用。
- 两个独立临时 PostgreSQL 探针应用当前 schema，验证重做、授权事件和 token 投影；未使用线上数据库。
- Node/Python 最小探针直接运行当前相关函数，验证附件丢失、撤销 revision 和无属主 AI 读取。
- 未复跑用户报告的所有测试，未进行真实拖动、双账号浏览器实操、线上迁移或插件安装。
- 修复 R1–R8 后应补上述失败场景回归，再做浏览器及安装包验收；现有测试通过不足以满足上线条件。
