# ux-formats 发布准备独立复核

日期：2026-10-04。应用候选 `d861dd1`，文档 HEAD `fa091d9`。本轮未切流、未启停生产容器、未执行迁移、未推送。只更新了 homePC 的操作员 helper 文件及其备份，应用候选镜像不变。

## 1. rc6 → 候选续跑：通过

从 `r1-rc6^{}`（`8127ecb3a9cdcb4c9a44f89546b25e5fb61effe1`）提取实际 runner/worker/WASM 及 C2 harness。旧版 harness 启动转换，在 journal 已写入 checkpoint 后注入中断并终止 worker；保持同一 Chromium origin、OPFS 任务和源副本，进入候选的真实 Flask `/tools/slides` 页面，通过任务列表“续跑”按钮完成。

| 输入/输出 | 已提交 checkpoint 字节 | 候选恢复结果 | 输出 sha256 |
| --- | ---: | --- | --- |
| 合成 KFB → bf-ome | 78,703 | ready，与原生基线一致 | `a16faa8a24c2700a4643e18af4705c68c5f13b456c8019ff16b2cbc7758d7b3e` |
| 合成 KFB → bf-classic | 78,703 | ready，与原生基线一致 | `065feb595af3fabddbd0bae92411e74b9a06886af3619a0f9fa8f376a7b8b91c` |
| 合成 KFBF → fl-ome | 51,874 | ready，与原生基线一致 | `c32be9e9155db7ad53903d1fa2b479f9963a919b99a41c3f3c492335bfc09abb` |

三个任务均沿用正确 output profile，恢复后编码策略为 `preserve-source-v1`；无需重新选择输入文件。私有 harness、提取的旧静态文件与 JSON 在 `.gate-tmp/rc6-upgrade-review/`。

范围：旧版实际转换器经 harness 建立任务、新版真实页面恢复。没有完整旧页面点击流程、OS 杀进程、Mac/Windows、上传中的跨版本恢复、新→rc6→新往返或真实低内存设备验收。不能将这三项扩大为任意历史版本任务兼容。

## 2. 发布 helper 回滚缺陷：已修

原 `rollback()` 无条件要求 PT_PRE 存在，并直接 inspect 标准 PT 容器。切换第一次 rename 成功、第二次失败时，PT 已不存在，恢复也随之失败。同样无法处理“旧容器停止后复查未通过”，或自身回滚过程中断在改名之间的状态。这些都是 runbook 停机后需要恢复服务的路径。

本地状态模拟器对原 helper 的结果：7 个恢复状态中 2 个通过、5 个失败。修复后：

- 恢复状态 7/7：旧容器仅停止、切换第一次改名后、候选改名但未启动、候选运行、回滚第一次改名后、旧容器改回但未启动、已回滚。
- 拒绝条件 5/5：当前镜像未知、旧目标镜像错误、缺旧目标、失败容器名冲突、staged 容器仍运行；均在任何 stop/rename/start 前拒绝。

这是状态模拟测试，不是生产容器故障注入。补丁：[deploy-rollback-20261004.patch](../review-evidence/slide-tools/UX/deploy-rollback-20261004.patch)。测试脚本位于上述私有目录 `test_deploy_rollback.py`。

同步 homePC 前检查原 helper SHA-256 与审阅副本完全一致；原文件备份为 `~/releases/suite-20261004/deploy.py.pre-rollback-review`，更新后 helper SHA-256：`3661e773ba0f304fbe3eefbca5223c8c854532a97b5dafc7890c7b78eecf329c`。只替换操作员脚本文件，没有调用其部署或回滚命令。

## 3. 文档收口与待决

真实 COS 验收报告末尾已改链到受版本控制的发布计划，不再依赖未跟踪的 agent prompt。发布计划更新了续跑证据、回滚边界和验证范围。

用户仍需决定 compact 盲评与 MRXS v2 视觉接受；Windows、真实低内存设备、真实 OS 保存对话框继续保留待验及是否阻断的决策。上述工程复核不替代这些判断。
