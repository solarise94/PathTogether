# 第三轮 review（2026-09-20）

结论：本轮确认 4 项 P1 和 1 项 P2，暂不建议上线。重点问题集中在附件实际消费和授权事件状态机。本次只执行 review、测试及临时探针，没有修改产品实现、提交或部署。

## T1 [P1] 冻结 render_context 没有从 wire 格式转换成内部格式

位置：`PathTogether/app.py:_validate_ai_attachments`；`HistoPilot/src/server.ts:parseAttachments`；`HistoPilot/src/agent-runner.ts:4903–4913`。

浏览器传来的附件上下文使用 `asset_revision` / `active_channels`。网关原样透传，sidecar 直接用类型断言当成内部 RenderContext，随后原样写入 image_ref。真正物化图像的客户端要求 `assetRevision` / `activeChannels`，`renderContextToWire` 会对不存在的 activeChannels 执行 map。

探针执行实际 applyAttachmentImageRefs → renderContextToWire，复现 `Cannot read properties of undefined (reading 'map')`。这会使带冻结通道的附件抓图失败或降级，不能保证用户看到的通道被送给模型。当前测试没有带真实 wire render_context。

修复：每项附件先经平台完整校验、绑定 asset revision、重算 fingerprint，再规范化到内部 RenderContext；不要用 TypeScript 类型断言代替转换。补两项相同 bbox、不同通道的最终模型请求测试，并验证错误上下文在请求边界明确拒绝。

## T2 [P1] branch 仍未消费附件

位置：`HistoPilot/src/agent-runner.ts:askBranch`、`driveBranch`。

server 虽然把 attachments 传给 askBranch，但正常新建/续聊分支没有将它存入消费队列；driveBranch 也没有调用 takePendingAttachments/applyAttachmentImageRefs。现有消费代码只在 driveMain。与 retry 相关的 Map 写入并不能补齐 branch 路径。

使用真实 AgentRunner、SessionStore、测试模型流完成同一附件的两条运行：main 成功保存 1 项 ref_att 引用；branch 正常 finished，却保存 0 项。因此在 marker 会话里右键加入其他视野仍会静默丢失。

修复：主会话、branch 新建和 branch 续聊共用持久化附件消费逻辑；用接收动作绑定附件，恢复/重试不能只依赖内存 Map。测试检查持久化消息及实际模型图像块，而不是仅检查 askBranch 入参。

## T3 [P1] 无新任务文本的 continue 会吞掉附件

位置：`HistoPilot/src/agent-runner.ts:1695–1701`。

takePendingAttachments 先从 Map 删除附件，随后仅在最后一条消息 role=user 时写入。一次正常回答完成后尾部是 assistant，用户添加新视野并按继续（无新 task）时条件不成立，附件直接丢失；后续循环补 continuation 消息已太晚。

真实 runner 探针：先 main 完成一个附件 A，再 continueMain 提交附件 B（不带 task）；结束后的 transcript 仍只有 A 的 bbox，没有 B。

修复：附件是本次用户输入，需为本次动作建立明确 user 消息，不能依赖上一轮 transcript 的尾部角色；成功持久化后再消费待发送队列。补“回答已完成→添加视野→不输入文字继续”的模型请求回归。

## T4 [P1] 曾经撤权的 marker 在重新授权后仍被判为 deleted

位置：`HistoPilot/src/agent-runner.ts:4935–4946`。

findSpot 的 revoked 是单向标志。平台全量 changes 会包含历史 access 事件；出现过一次 revoke 后，即使后来 grant 且当前已经允许读取真实 ROI，revoked 也不会恢复。结果重新分享的标注仍不能开启分析，grant 的权威几何回填也会被跳过。

真实 findSpot 探针输入 `[live ROI, grant, revoke, grant]`，返回 `{annotation_id:'mark', deleted:true}`。

修复：按事件顺序与当前权威可见集合解析有效权限。最终 grant 只有在重新校验并取得当前可见 ROI 后才能恢复；不能把任意历史 revoke 永久当作 tombstone。补 grant→revoke→grant、多个授权来源及真实删除/恢复的组合测试。

## T5 [P2] 新增测试没有使用临时会话目录，也没有等待后台任务结束

位置：`HistoPilot/test/access-and-attachments.test.ts:84`，同文件另外两处 SessionStore 构造也相同。

`new SessionStore(dir)` 传入字符串，但构造函数要求 `{sessionsDir: dir}`。运行时读取不到 opts.sessionsDir，因此使用环境指定目录或 `~/.histopilot/sessions`，并非 mkdtemp 创建的目录。附件测试只等待 runMain 接受，不等后台 settle，可能残留 running 会话。

本次组合复跑发生 `SessionConflict: 会话正在运行中`：19 项通过，1 项失败。该测试还只断言入参和任务文字，不能证明附件进入模型图像请求。

修复：改用正确构造参数和现有 isolated harness；等待 settle 后断言最终请求，并用 finally 清理仅属于测试的临时目录。重复执行两次和与相关测试组合执行都应通过，不能接触默认业务会话目录。本轮未删除默认目录中的任何会话。

## 本次验证

- PathTogether：`.venv/bin/pytest -q tests/test_annotation_isolation.py`，24 passed。
- HistoPilot：`npx vitest run test/access-and-attachments.test.ts test/ui-attach-draft.test.ts test/fork-overview.test.ts`，19 passed / 1 failed，失败点见 T5。
- 实际函数探针验证 wire context 转换异常和重新授权仍被判 deleted。
- 隔离临时 SessionStore + 真实 runner + fake model 验证 main 有附件、branch 无附件、无文本 continue 不追加新附件。探针自己的临时会话目录已清理。
- 未做浏览器、线上库迁移、插件安装或完整测试集复跑；本轮没有重新验证所有历史 review 条目。
