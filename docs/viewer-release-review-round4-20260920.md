# 第四轮代码 review（2026-09-20）

范围：当前工作区第三轮修复，重点为附件转换、恢复执行、授权事件及测试验收。未修改产品代码，未提交、部署或执行数据库迁移。

结论：发现 2 项 P1、1 项 P2。正常 main/branch 发送及无文本 continue 的定向回归通过；异常恢复和渲染身份仍有缺口。

## U1 / P1：branch 同 request_id 恢复仍丢附件

位置：HistoPilot/src/agent-runner.ts:2264–2276；相同遗漏还存在于 retryExistingAction 的 fork/branch 分支。

askBranch 命中 retry 后直接进入 retryExistingAction。已 dispatch 的动作由 resumeDispatchedAction 启动 driveBranch，但该分支没有把 args.attachments 放入队列；只有下方 main 分支有这一步。普通新建和续聊入口增加的入队逻辑被提前返回绕过。

隔离探针沿用仓库 request-id-idempotency 测试的崩溃模拟方式：创建 branch、markRequestAccepted、markRequestDispatched、setStatus(paused)，然后使用同 request_id 调 askBranch，重试请求明确携带 bbox=(901,902,20,20) 的附件。等待完成后的结果为 status=finished、sameSession=true、attachmentCount=0；只有 ref_overview0 和根标注 ref_fork_r4-mark。

修复建议：统一新建、续聊、恢复的附件入口。将冻结附件纳入持久化 action_input，恢复优先使用已接受动作的数据；在用户消息成功落盘后才标记消费，并保证同请求重复恢复不重复追加。当前 takePendingAttachments 会先删除内存队列，并非报告所述的“持久化成功后消费”。

验收：main/fork/branch 在 dispatch 后、附件消息落盘前模拟重启；同 ID 重试均保留原附件且恰好追加一次，同时覆盖重试请求不重传附件的情况。

## U2 / P1：附件上下文缺少可信 fingerprint 时会串渲染缓存

位置：HistoPilot/src/platform/contract.ts:295；入口 HistoPilot/src/server.ts:1461；缓存键 HistoPilot/src/transform-context.ts:352。

renderContextFromWire 接受缺少 fingerprint 的上下文并填空字符串，也原样信任调用者提供的 fingerprint。PathTogether 的 _validate_ai_attachments 原样转发每项 render_context；sidecar 新增转换并未为附件建立可信渲染身份。衍生图缓存只把 renderContext.fingerprint 作为通道/平面上下文的区分键。

隔离探针调用真实 renderContextFromWire、overviewDerivativeSpec 和 materializeDerivativeRaw，仅 region 使用返回颜色字节的 mock：同片、同 bbox、同版本，A 红色通道、B 绿色通道，两者都省略 fingerprint。两者均被接受，fingerprint 均为空；region 只调用一次，A、B 最终都返回红色字节。第二项命中缓存时不会再经过平台的 region 校验。这是可重复的错误视觉输入，不只是输入校验宽松。

修复建议：对每项附件规范化完整上下文，并按平台既有算法生成/核对 fingerprint，或从已验证的完整上下文生成可信缓存身份。仅要求非空仍不能防止不同上下文携带相同 fingerprint。避免破坏历史 transcript 的兼容读取，可将严格请求入口与历史解析分开。

验收：同片同框、不同颜色/plane/channel 的附件必须得到不同衍生图；缺失、伪造或复用 fingerprint 的请求应拒绝或规范化。测试既覆盖顺序缓存，也覆盖并发去重。

## U3 / P2：新增回归测试未通过默认 TypeScript 检查

位置：HistoPilot/test/access-and-attachments.test.ts:59–64。

flask.spots 的替代实现不满足 Promise<ChangePage>：fixture 的 ROI 缺少 index/token/slide 等必填字段，access 事件也需与实际事件类型对齐。默认 npx tsc --noEmit 报 TS2322；Vitest 转译执行通过不能代替类型检查。

修复建议：用符合真实事件契约的 fixture，并使访问事件类型与实际返回联合类型一致，避免用粗粒度类型断言掩盖差异。

默认类型检查还有其它文件的错误，本轮不将那些错误全部归因于本次修改。src 构建范围的 npx tsc --noEmit -p tsconfig.build.json 通过，验收报告需明确命令和范围。

## 本轮验证

- npx vitest run test/access-and-attachments.test.ts test/ui-attach-draft.test.ts test/fork-overview.test.ts：3 文件、20 测试通过。
- npx tsc --noEmit -p tsconfig.build.json：通过。
- npx tsc --noEmit：失败，包含上述新增测试错误及其它测试文件错误。
- branch 恢复丢附件、不同上下文缓存碰撞：独立隔离探针复现。
- 未复跑全部 1450 项、未进行双账号浏览器/真实右键与拖动验收、未执行迁移演练。
