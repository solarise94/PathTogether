# R12 原始审查证据

这里保留对 PathTogether `8d49fcb` 的审查记录、四个失败反例及原始输出。原记录中的 `/tmp/slide-id-review-r12` 是当时的执行位置，不是后续执行依赖。

`test_r12.py.txt` 是原始源码，使用文本扩展名避免自动测试收集。执行 Agent 按 [修复方案](../../r12-capacity-lifecycle-fix-agent-plan-20260927.md) S0 复制至 `tests/test_slide_id_review_r12.py`，使用仓库已有 conftest 启动独立 PG；不复制历史 conftest。原始输出中的临时路径和随机任务 ID 只作证据。

这些失败记录不是最终验收测试。文件互斥协议与核账合同修改后的测试调整规则见修复方案第 6 节；保留本目录证据，不覆盖为修复后结果。
