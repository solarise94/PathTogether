# 测试债清理与验证记录（2026-10-08）

基于 `admin-viewer@46fb4a5e`，分支 `codex/test-debt-cleanup`，工作树 `.gate-tmp/test-debt-wt`。只修改测试、测试入口配置和本文档；没有修改业务实现、迁移、生产数据，没有 push 或部署。

## 完成的清理

| 问题 | 修改与覆盖 |
| --- | --- |
| 默认 pytest / Vitest 误收集 | pytest 默认目录限定 `tests`；Vitest include 限定 `tests/js/**/*.test.ts`；插件显式独立运行。删除 `pg_compat` 及其 conftest 常量依赖；后端 canary 直接检查生产选择器和真实 SQL。平台与插件联合收集也已验证。 |
| PostgreSQL 已单轨，仍保留双后端分支 | 展开固定 PG 分支，删除死 JSON 分支、无用 BACKEND 导入、空的登录限流 mock 及参数；更新运行说明。保留迁移、并发、计费、过期门禁等真实行为测试。 |
| 重复助手 | 多处 Flask client / dict-row PG 连接统一到 `_pt_helpers`；四个简单认证 JS harness 复用 `helpers/basic-element.ts`。不同画布、布局、事件语义的桩没有强行合并。 |
| 永久 skip 与空占位 | 删除已退役 `_api_upload_native_single` 的永久 skip 用例；四个无论配置如何都会 skip 的百度真人验收占位移入 `tests/README.md` 的 L01–L04 人工清单。真实样本/CLI 条件跳过保留。 |
| 过时 E2E 契约 | MRXS 改按 convert 契约；不同用户采用独立 browser context；固定 UI 语言；能力接口校验公开字段与数字 limits，允许合法公开原因码 `secret_unconfigured`。 |
| BMP 用例长期卡在不可用 COS 上传 | 通过生产发布函数预置真实 TIFF/BMP；每例独立资产；使用稳定 ID API 与真实瓦片、像素、自动保存及刷新回放。真实 COS 传输未由这些离线用例覆盖。 |
| COS 异步偶发失败 | 等待实际并发请求/终态，保留真实 WebCrypto；使用假时钟控制重试与轮询；teardown 取消并等待上传 controller。额外连续运行 5 次全部通过。 |
| 首页动画慢 | 用可控 RAF 帧推进真实动画，MutationObserver 验证全部 8 个自动场景；保留原生时钟的文字渐进、暂停、语言切换用例。首个动画用例从约 48 秒降到约 1 秒。 |
| 无效源码字符串断言 | 删除全文搜索 logout/POST/permissions/setRole 的弱测试；改为实际请求 POST+CSRF、分享 ID/permissions payload、effective role 注入断言。 |
| Viewer 测试使用错误 DTO / 只复述公式 | 到期值改为列表中的 epoch 秒，info 不含标记；验证有效期与到期自动清屏。搜索场景混排文件夹与切片；空间利用直接检查显示数量。 |
| AI 授权测试漏掉上传者 | 保留管理员授权撤销的成功参数，再加入上传者持有同切片授权且不应被撤销的参数。 |
| 测试自身的废弃 API 警告 | Pillow 像素等价比较使用 mode/size/bytes，去掉废弃 getdata；有意构造重复 ZIP 路径时显式捕获并断言对应警告。 |

## 运行结果

| 执行范围 | 结果 | 说明 |
| --- | --- | --- |
| 平台全量 pytest | 2929 passed / 113 skipped，475.32s | 没有失败；原始日志有 315 warnings。 |
| 最后补充/修改的 Python 文件 | 49 passed / 1 strict xfailed，7.41s | 全量收集后增加了上传者授权隔离参数，并清理 Pillow/ZIP 警告；完整重跑 `test_admin_temporary_view.py`、`test_raster_viewing.py`、`test_zip_guard.py`。这 49 个通过与全量重叠，不相加。 |
| JS 全量 | 964 正常通过 / 3 已知预期失败 / 2 条件跳过，5.34s | Vitest 原生汇总是 967 passed / 2 skipped，其中含 3 个 `it.fails`，不能当作业务正常通过。 |
| Playwright 全量 | 78 正常通过 / 2 已知预期失败，68.84s | 80 个实际执行；0 unexpected、0 flaky、0 skipped。原基线为 73 passed、5 failed、1 did not run。 |
| 百度插件 pytest | 46 passed / 37 skipped | 跳过因 native CLI 未构建。 |
| COS 前端定向连续运行 | 5/5 次成功，每次 12 个用例 | 与其他测试并行负载下运行，未观察到原偶发失败。 |
| 平台与插件联合收集 | 3125 项成功收集 | 此后新增一个上传者权限参数；根目录入口不再有此前的 conftest 收集冲突。 |
| 静态校验 | Python AST、`git diff --check` 通过 | 没有业务源文件变化。 |

剩余 310 个第三方警告来自当前锁定的 tifffile 与 NumPy 版本组合。未通过全局过滤掩盖警告，也没有在测试清理中升级受 zarr 兼容约束的生产依赖。CLI/真实样本缺失的跳过均不算通过。

## 仍由 Opus 修复的业务问题

六个复现均实际执行，使用严格预期失败标记；修复后需要移除标记。前四项是此前 review 的问题，后两项是复活旧 E2E 后新发现的问题。

| 问题 | 回归位置 |
| --- | --- |
| 结束管理员临时查看误撤销上传者 AI grant | `tests/test_admin_temporary_view.py` 的 `preserve_uploader=True` 参数；正常管理员撤销参数仍通过。 |
| 临时查看缺少从列表携带到期值，且秒/毫秒解析错误 | `tests/js/viewer-folders.test.ts` 的 epoch 秒有效期/自动到期场景。 |
| 搜索定位页码漏算前面的文件夹 | 同文件的混合目录搜索场景；目标卡实际不可见。 |
| 文件夹计入空间方式导致一页仅一张卡 | 同文件的多文件夹可用空间场景；600px 中只显示一张。 |
| 分享选择器确认后，分享浮层被同一次冒泡点击关闭 | `tests/e2e/raster-image-compat.spec.ts` 的独立选择器用例。 |
| 新发布、仅有 ID 的资产不能保存分享标注 | 同文件的分享页用例：请求带正确 slide_id，路由通过 ID 校验，store 却用空 legacy filename 判成员，返回 400 `slide not in share`。 |

三个 Viewer 用例曾临时移除 `it.fails` 执行，确认失败点分别是可见卡只有 1 张、目标卡不存在、到期仍有打开的切片；执行后恢复严格标记。它们不是因 harness 崩溃或收集错误而“符合预期”。

合并业务修复时，应保留真实 DTO、混合目录与跨用户授权断言，删除对应预期失败标记。详细复现和常规运行入口见 [tests/README.md](../tests/README.md)。

## 本地证据

工作树下 `.gate-tmp/test-debt/` 保留 `pytest.log`、`final-targets.log`、`vitest.log`、`playwright.json`、`plugin-pytest.log`、`viewer-known-failures.log` 和 `cos-repeat-1.log` 至 `cos-repeat-5.log`。浏览器失败 trace/截图位于同目录 `e2e-full/`。这些本地工件不进入提交。
