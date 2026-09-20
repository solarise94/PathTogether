# 第二轮上线前 review（2026-09-20）

结论：仍不建议上线。确认 5 项 P1；部分上一轮问题已改变表现，但尚未闭环。本次仅审查、执行探针并新增本文档，没有修改产品实现、提交、迁移线上库或安装插件。

## Q1 [P1] 未验证的 session_id 可以覆盖既有会话读取属主

位置：`app.py:13308–13315`；`share_store_pg.py:1294–1310`。

`_ai_run_prepare` 在请求交给 HistoPilot 校验会话归属之前，直接用请求体的 session_id 写入本地 principal；UPSERT 在冲突时覆盖 user_id 和 slide。B 提交 A 的 session_id，即使 sidecar 随后拒绝该请求，本地读取身份已经改成 B，且拒绝回调没有恢复它。A 后续 spots 的权限就可能按 B 判断，污染 A 的模型上下文；同时原主体的读取会异常。

临时 PostgreSQL 探针：先绑定 victim-session→user-A，再绑定相同 session→user-B，查询结果为 user-B。

修复：删除未验证请求体触发的预绑定；绑定只能由服务端确认的会话身份建立。已有 session 的 owner/slide 必须不可被冲突写入改绑，并在读取时检查 slide 与主体有效权限。lite fork 首次启动也应通过可信的同步绑定协议建立身份，不能依赖返回 SSE 响应后才执行的回调与后台 runner 恰好有利的时序。

回归：B 向 A 会话发 run/continue 被拒后，principal 仍属于 A；失败请求不能预占他人的会话 ID；首次 lite fork 的第一条 spots 请求已能识别合法属主。

## Q2 [P1] 评论和评论增量仍泄漏来源分享 bearer token

位置：`share_store_pg.py:1390–1397`、`list_comments:2384–2391`；`share_server.py:share_comments_list`。

ROI 白名单生效，但评论仍从 data 原样复制返回。若 S1 标注带评论，被授权给 B 或 S2 后，对方可以从评论的 token 字段取得 S1，继续访问原分享链接的其他内容。分享页仅掩码作者，没有去除 token。

临时 PostgreSQL 探针：S1 标注→新增评论→授权 B；`list_comments(subject=B)` 和 `list_changes(subject=B)` 的评论均包含 S1 的完整 token。

修复：增加评论的白名单响应投影，覆盖工作台列表、分享列表、评论增删事件及相关历史/返回值。去除来源 bearer、内部身份字段；仅允许保留调用方本来已持有的访问凭据。

回归：同一条跨链接/用户授权的标注既有 ROI 又有评论时，检查所有 HTTP 响应而不仅是 annotations 列表。

## Q3 [P1] 删除成功后未更新 revision，正常重做仍然失败

位置：`static/app.js:4550–4561`、`4596–4602`、`performRedo`；`app.py:api_annotation_delete_by_id`。

创建 revision=1 → DELETE 成功后数据库 tombstone revision=2，但删除 API 不返回新 revision，前端 sendAnnoDelete/undoCreateEntry 也不更新 entry.revision。重做 restore 仍提交 expected_revision=1，因此正常单人流程也返回 409。改走 restore 解决了唯一键问题，但没有解决前后端状态衔接。

临时 PostgreSQL 使用真实 store：删除后拿原 revision 恢复得到 RevisionConflict(current_revision=2)，改用删除后的 revision 才恢复成功到 3。

现有 `test_restore_after_delete_same_client_action_id` 在删除后直接读数据库 tombstone，再用它的 revision 请求恢复，所以绕过了真实浏览器缺少该版本的问题。

修复：删除事务返回权威 tombstone revision；API 透传、前端解析并更新历史条目后再进入重做栈。增加完整前端 POST→DELETE→restore→DELETE 循环测试，不由测试代码额外读取数据库补版本。

## Q4 [P1] HistoPilot 丢弃附件数组，模型仍未收到完整附件

位置：`HistoPilot/src/server.ts:handleRun/handleContinue/handleBranch`；`PathTogether/app.py:17630–17631`。

插件与网关已经发送 attachments，但 HistoPilot 的 src 中没有附件解析/消费实现。`handleRun` 只传 task、viewport 等旧字段给 runner；附件数组被忽略。多张卡片仍不能作为多项图像证据进入模型，marker ID/revision 也止步于网关校验。

Node ESM 探针直接调用真实 SidecarServer.handleRun（仅 mock 输入与 runner）：输入两项 attachments，runner 收到的参数没有 attachments。

修复：接通 sidecar schema、runner 参数、各项冻结 render_context、marker 服务端几何/备注、图像物化与持久化请求组装；明确 run/continue/branch 的一致行为。测试应拦截最终模型请求，确认两项位置与通道不同的附件都存在，不只断言插件 POST body。

## Q5 [P1] 新 access 事件被当作 ROI，覆盖真实标注并注入零坐标

位置：`HistoPilot/src/agent-runner.ts:4220–4243`、`findSpot:4818–4833`。

新增 access 事件没有几何。injectSpotChanges 只特殊处理 revoke，grant 会落入普通 spot_updated 分支，输出 (0,0)、0px。findSpot 则按同 annotation_id 的最后一条变更取值，不区分 access 和 ROI；全量读取中后发生的 grant/revoke 会替换真实 ROI。撤权通知本来就应在失去读取权限后可见，但不能据此被当作仍可读取的根标注。

Node ESM 探针执行真实方法：changes=[真实矩形, access grant] 时 findSpot 返回 access grant；只收到增量 grant 时 injectSpotChanges 生成“左上角 (0,0)，边长 0px”的标注线索。

修复：access 与 ROI 分别处理。grant 应重新获取当前授权下的权威标注或触发集合重建；revoke 应清除引用并使 root 查找失败。findSpot 必须忽略通知作为几何候选，且不能回退使用已撤权的旧 ROI。补“grant→从共享 marker 开分析”和“revoke→继续旧分支”测试。

## 验证结果与边界

- PathTogether：`.venv/bin/pytest -q tests/test_annotation_isolation.py`，22 项通过。
- HistoPilot：`npx vitest run test/ui-attach-draft.test.ts test/fork-overview.test.ts`，17 项通过。
- 独立临时 PostgreSQL 应用当前完整 schema，复现 principal 覆盖、评论 token 泄漏和恢复版本冲突。
- Node ESM 探针运行当前真实 SidecarServer.handleRun、AgentRunner.findSpot/injectSpotChanges，复现附件丢弃和 access/ROI 混淆。
- 上述现有测试通过与失败探针并存，说明回归测试尚未覆盖实际端到端链路。
- 未做浏览器、线上迁移或安装包运行验证；也未复跑用户报告的全部测试。

关于已声明的撤权限制：保留审计历史和在后续模型请求中继续发送历史图像/文本是两件事。目前追加 spot_deleted 文本并未实现后续请求的数据剔除，不能把它描述为已完成 AI 上下文撤权。当前作为未闭环限制保留，后续应验证 prepared request/checkpoint 的实际内容。

关于 🌐 语义：本轮按用户说明接受“所有包含本片的活跃分享”为既定产品范围，不把缺少逐个选择作为问题；界面应清楚显示该授权范围。
