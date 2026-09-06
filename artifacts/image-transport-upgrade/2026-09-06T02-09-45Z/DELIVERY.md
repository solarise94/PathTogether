# image-transport-upgrade 交付（§1.3 状态对象 + G1–G9）

code_complete: true
implementation_complete: true（G7 两项 blocked 如实记录：①真实荧光样本 2/3 张；②精细档浏览器整轨迹 bytes 比 1.445 > 1.35 预算——图像级口径 q84=1.231 达标；不重命名不算成功）
release_verified: false（未部署，无授权）
blocked: ["真实荧光样本 2/3（缺第 3 张，WKL 传输中不充数）", "精细档浏览器整轨迹 bytes 预算 1.445>1.35（图像级达标，质量/预算取舍需产品决策）", "G9 线上验收未执行（无部署授权）"]
commit: PT 826f40b + 600c8e3（wip/ser8-dev）；HP 无变更 b9d7805
push: 未做
CI: 未运行（本地无 CI 触发；本地全量门禁已过）
deploy: 未做（release/deploy.sh + rollback.sh + RUNBOOK.md 就绪）
online_verification: 未做（release/RUNBOOK.md G9 清单待执行）

## G1–G9
- G1 ✓ B0-baseline.md：双仓 HEAD/干净、依赖（Pillow 12.3.0 等）、编码调用点审计、
  样本盘点、worktree 偏差记录；diff 可审（73 文件主提交）
- G2 ✓ 编码/荧光契约：§3–4 全部测试通过（test_display_jpeg_encoding 等）；JPEG 实际
  采样断言（解码级）；thumbnail 无隐含 4:2:0（荧光显式 4:4:4）；region golden 字节护栏
- G3 ✓ 版本/缓存：文件换代、env 质量换代键、ETag/304/撤权、旧缓存迁移语义、
  bytes LRU、single-flight（含异常/有界退出）；test_slide_cache_generation 19 项
- G4 ✓ UI/E2E：三入口 spec + channel-panel 8 项通过；DPR=1/2 轨迹；乱序/409 合并在
  JS 单测锁定；真实网络缓存头由 pytest 真实 Flask+PG 覆盖；HTTPS 迁移留 G9
- G5 ✓ region 字节对拍（基线 worktree vs 实施 checkout：全部 sha 一致，仅随机会话
  token 签名异）；HP 1296 + contract 41；checkpoint gen1→gen2 回归通过；HP 零变更
- G6 ✓ 全量：PT 1575 passed/0 failed/0 skipped、coverage 83.84%；JS 281；E2E 37
  passed + 1 既有失败（基线复现）；HP 1296；canary 3；diff --check 双仓 ✓
- G7 部分：真实 RGB/荧光数字见 B6-results.md；两项 blocked（如上）
- G8 ✓ 本地回退演练 7/7（rollback-drill.log.txt）；AI session 零写入；自审清单全过
- G9 ✗ 未执行（无部署授权）；RUNBOOK.md 含完整清单
