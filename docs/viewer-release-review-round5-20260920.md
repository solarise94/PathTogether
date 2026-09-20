# 第五轮代码 review（2026-09-20）

范围：第四轮 U1–U3 修复。未修改产品代码、提交或部署。

结论：发现 2 项 P2；本轮未确认新的 P1。上一轮 branch 在初始消息落盘前恢复丢附件的路径已有回归覆盖，颜色不同的上下文不再沿用不可信输入指纹。仍需补齐首次消息的幂等标记，以及与 Python 平台的指纹兼容性。

## V1 / P2：首次附件落盘后、动作结束前恢复会重复追加

位置：HistoPilot/src/agent-runner.ts:2413（首次 branch），1965（首次 fork）。

首次消息调用 applyAttachmentImageRefs，只取 pending.atts，丢掉 pending.key，没有写入 attachments_dispatched。若附件消息已经落盘，但进程在动作 settle 前退出，恢复会从 ledger 重新入队原附件；此时 driveBranch/driveFork 走已有消息分支，hasDispatchedAttachment 找不到标记，再追加一次。

独立探针通过正常 askBranch 创建带附件、带 request_id 的会话，在 runGuardedLoop 入口停止执行（此时初始消息已持久化），把状态转 paused 模拟重启，然后恢复正常执行并用同 request_id、不重传附件调用 askBranch。结果：status=finished；附件由 before=1 变成 after=2；初始消息标记为 null，新增消息才带该 request_id。

这与“已完成动作重发应 dedup”是不同窗口。现有回归在第一次恢复结束后使用新 request_id，无法验证同一个未完成动作在消息落盘后的恢复。

建议：首次消息和续聊消息共用附件消费标记规则，key 随 image_ref 在同一次写入中落盘；检查 main/fork/branch 首次路径。新增回归在首次消息落盘后、settle 前暂停并恢复相同请求，断言附件仍为 1，模型请求中也不重复。

## V2 / P2：重算的指纹与平台真实算法不一致

位置：HistoPilot/src/platform/contract.ts:299；序列化实现 canonicalValue；对照 PathTogether/slide_render.py 的 _round4 / _fingerprint。

本轮 renderContextFromWire 把平台已经生成的 fingerprint 全局替换为 TypeScript 重算值。但 Python 规范化将 alpha/black/white/gamma 转为浮点，json.dumps 会保留整数浮点的 .0；TypeScript canonicalValue 使用 String(number)，会省略 .0。即使输入已由平台规范化，跨语言哈希仍不同。

使用 PathTogether 实际 canonicalize_render_context 函数生成合法上下文（大写红色，通道 0，alpha=1、black=0、white=255、gamma=1，plane=0/0），将其 JSON 交给真实 renderContextFromWire：

- 平台 fingerprint：e58df72df506985132de023d089be0a9c008658864c31b753c5062a4c9ee7b9f
- sidecar 重算：33a8f1c130ffce15fb769949452caf76fc97b8ca64a57f20fc03edaf93eead72

影响：合法平台默认上下文和附件转换后改变视觉身份；同一渲染在平台来源与 sidecar 重算来源之间无法共享同一指纹/缓存身份，跨端依赖指纹相等的比较不可靠。这不是新的红绿图缓存碰撞，本轮也未据此断言图片内容错误或线上绘制失败，因此定为 P2。

此外，TypeScript 当前解析没有执行平台的颜色大写、通道排序、四位小数规范化；不要只针对示例补一个 .0 后就宣称算法一致。

建议：建立真正兼容平台的 canonicalization 和序列化，或通过可信平台入口取得规范化上下文与指纹。保留 U2 的不信任任意输入指纹原则；不要简单恢复原样信任。跨语言 golden fixture 必须由 Python 实际生成，至少覆盖整数浮点、小数、颜色大小写、通道顺序、native RGB，并考虑已持久化上下文的兼容性。

## 验证范围

- npx vitest run test/access-and-attachments.test.ts test/platform-contract.test.ts test/http-client.test.ts：3 文件、87 项通过。
- npx tsc --noEmit -p tsconfig.build.json：通过。
- 隔离 branch 中断恢复探针：确认重复附件。
- Python 平台真实 canonicalization → JSON → TypeScript parser：确认指纹不同；未访问线上库或线上切片。
- 未复跑全量 1451 项或默认全项目 tsc；未做浏览器、双账号、右键与拖动验收或迁移演练。
