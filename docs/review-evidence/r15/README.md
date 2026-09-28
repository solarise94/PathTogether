# R15 独立复核证据

这里归档对 `a668580` 的独立复核记录、两个失败反例和输出。报告内 `/tmp/slide-id-review-r15` 是原执行位置，不是后续运行依赖。

`test_r15.py.txt` 为原始源码，避免文档副本被 pytest 自动收集。执行 Agent 可将它复制为 `tests/test_slide_id_review_r15.py`，使用现有 tests/conftest.py；不要复制另一份 conftest。若 HEAD 已修复，先验证后继续，无需重复实现。

后续目标见 [统一 COS 上传执行方案](../../cos-only-upload-agent-plan-20260928.md)。原始失败证据保留，不覆盖为新测试结果。
