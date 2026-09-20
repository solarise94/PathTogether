# 第六轮 review（2026-09-20）

本轮复核 V1/V2 修复，未改产品代码或提交。发现 1 项 P2，未确认新的 P1。

## W1 / P2：乘 10000 后判断 half-even 不等于 Python round(float, 4)

位置：HistoPilot/src/platform/contract.ts:194–200。

pythonRound4 先执行 v * 10000，再判断与整数之间的差是否为 0.5。乘法本身会再次舍入，可能把原始 IEEE-754 值位于十进制中点上方/下方的信息抹掉。因而即便规则名都叫 half-even，也不能复制 Python round(float(v), 4)。

独立跨语言探针：用 PathTogether 的真实 canonicalize_render_context 处理合法单通道上下文，再将相同原始输入交给 TypeScript renderContextFromWire。

| 原始 alpha | Python 规范化 | TypeScript 规范化 | 指纹相等 |
| --- | --- | --- | --- |
| 0.12345 | 0.1235 | 0.1234 | 否 |
| 0.00005 | 0.0001 | 0 | 否 |
| 0.10005 | 0.1001 | 0.1 | 否 |
| 0.50005 | 0.5 | 0.5 | 是 |
| 0.99995 | 1.0 | 1 | 是 |

触发条件是请求附件包含尚未由平台规范化的合法高精度通道参数；平台 _validate_ai_attachments 原样传递各项 render_context，sidecar 负责转换。此时不仅指纹不同，实际发送给 region 的规范化参数也不同。对上述由 Python 先规范化后的上下文再做 TypeScript 转换，指纹全部相等，因此不把该问题描述为所有默认上下文仍失败。

0.12345 与 0.10005 均在普通取值范围内，和科学计数法极端值无关。

建议：以原始 binary64 的精确值实现与 Python 一致的十进制位数舍入，或在可信平台统一完成规范化。不要用固定 epsilon 修补，亦不要只把输入字符串按十进制 half-even 处理，两者均无法保证与 Python float 语义一致。增加由真实 Python 生成的临界值 golden，覆盖上述值、负窗口值，以及中点两侧相邻浮点值，并同时断言规范化字段与 fingerprint。

## 已验证

- 首次附件消息现在携带 request_id 消费标记，fresh main/fork/branch 和 main overview 重建路径均已传递 key；新增中断窗口回归通过。
- 定向三文件：89 测试通过。
- npx tsc --noEmit -p tsconfig.build.json：通过。
- 跨语言探针确认普通已规范化上下文的 Python/TypeScript 指纹相等，原始中点输入仍有上述差异。
- 未复跑完整 1453 项、默认全项目 tsc、浏览器双账号/真实拖动或迁移演练。
