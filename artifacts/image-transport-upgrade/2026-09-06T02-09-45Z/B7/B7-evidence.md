# B7 全量门禁与本地回退演练

## G6 全量检查（最终一轮，含 demo 测试去 skip 修复）
- PT：`python -m pytest tests -q --cov=. --cov-report=term --cov-report=xml --cov-fail-under=72`
  → **1575 passed / 0 failed / 0 skipped**，coverage **83.84%**（≥72 达标）
- PT JS：`npm run test:js` → 19 files / **281 passed**
- PT E2E：`npm run test:e2e:admin -- --project=chromium` → 37 passed +
  1 failed（admin-workbench "20. 390 overview"——**既有失败**：stash 全部本次改动后
  在基线 HEAD 7583736 上同样失败，已复现确认）+ 2 did not run（同 spec 级联；
  "21/22"单独运行 **2 passed**）
- PT：`git diff --check` ✓
- PG canary：`pytest tests/test_pg_backend_canary.py -q -rA` → 3 passed
- HP：`npm test` → 63 files / **1296 passed**；`npm run build` ✓；
  `PATHTOGETHER_REPO=... npm run test:contract` → 3 files / **41 passed**；`git diff --check` ✓
- 跨仓结论：新 PT + 当前 HP 全兼容；HP 生产代码零变更

## 本地回退演练（rollback-drill.log.txt，7/7 通过）
1. 旧服务端 legacy tile：200 + `public, max-age=31536000, immutable`（升级前基线行为）
2. 新服务端同 legacy URL：200 + `private, no-cache` + 强 ETag
3. If-None-Match → **304，body=0**（鉴权/版本核验后）
4. 版本化 URL（profile+dv 取自 info）→ 200
5. 过时 dv → **409 display_version_conflict + no-store**
6. 版本化 URL 打到旧服务端 → 200（旧 worker 忽略未知 query，646B）——回滚后页面不会
   无限刷新；须按 RUNBOOK 验证页面实际只发旧协议请求
7. 新旧 legacy tile 字节 **IDENTICAL**（cmp 通过）——回滚画面零变化
- AI session：演练全程只读（tile/info 请求），无 session/checkpoint/权限数据写入

## 自审清单
- 参数传播：URL profile/dv → parse（白名单+成对+格式+重复）→ resolve spec → 编码/键/dv 同源 ✓
- 鉴权：三入口 tile/thumbnail 均先鉴权再参数校验与缓存查找；304 在鉴权与版本核验之后；
  撤权（share revoke）同 ETag 不 304（测试通过）✓
- 缓存身份：键=(safe,gen,fp,level,x,y,fmt,encoding_fingerprint)；编码与键同一 spec；
  env 质量换代即换键（B1 回归覆盖）；generation/read_stable 换代纪律保持 ✓
- 资源版本：JS ?v=20260906a；新增 viewer-encoding.js 三模板引入；入口 HTML no-cache ✓
- 错误终态：409 客户端有界恢复（二次冲突停止）；single-flight 等待者有界退出→503；
  错误响应 no-store ✓
- 内存计数：bytes 账本（含 share TTL 包装 sizer）；同 key 覆盖差额记账；超预算不缓存；
  竞争淘汰不变负（单元测试覆盖）✓
- 测试跳过：全量 0 skip（demo display 测试改为门直通真正断言）✓
