# R1 一键转换并上传 复跑指南

日期：2026-09-30。全部命令在仓库根 `PathTogether/` 执行；`TMPDIR=$PWD/.gate-tmp`
（/tmp 是小 tmpfs）。证据 JSON/日志在本目录 `results/`。报告：
`docs/slide-tools/r1-convert-and-upload-report.md`。

前置：原生 CLI 已构建（缺失时 `PATH=$HOME/.cargo/bin:$PATH bash
scripts/build_slide_transform.sh`）；`.venv` 可用；node ≥20（仓内 node_modules 含
vitest/playwright，无新下载）。浏览器测试彼此独立起服务/浏览器，但**不要与全量
pytest 并行**（CPU 争抢会让转换类场景超时）。

## 1. R1 浏览器 e2e（本报告主证据；27 场景：§3.1 验收 a–j + 审查回归 i5 + 二轮修复 d4/d5/k1 + 上线 dogfood 回归 l1–l3）

    node tests/browser/slide_tools_r1/run_e2e.js              # 全部 27 场景（约 32 min）
    node tests/browser/slide_tools_r1/run_e2e.js --only a-oneclick-order-network
    node tests/browser/slide_tools_r1/run_e2e.js --only i5-assoc-failure-retry   # 审查回归
    node tests/browser/slide_tools_r1/run_e2e.js --only k1-published-repeat-clicks
    node tests/browser/slide_tools_r1/run_e2e.js --only d4-two-users-separate-upload
    node tests/browser/slide_tools_r1/run_e2e.js --only d5-two-users-return-to-original
    node tests/browser/slide_tools_r1/run_e2e.js --only l1-project-open-and-ui-delete  # dogfood P1
    node tests/browser/slide_tools_r1/run_e2e.js --reuse-server   # 复用已在 :8963 的服务

预期：逐行 `PASS [a-oneclick-order-network] … PASS [l3-drawer-offer-clickable]`，
末行 **E2E ALL PASS**（27/27）；证据 `.gate-tmp/slide-tools-r1/e2e/results.json`
（本目录 `results/r1-e2e.{txt,json}`）。

被测应用：`tests/browser/slide_tools_c4/server.py`（R1 启动时加 `--fake-cos-worker --seed-ready-slide`：第二个普通用户、进程内只做 Initiate 的假 COS 让真实 ingestion 到 uploading、一个真实 ready 切片供真实项目关联、两张同原始文件名的 ready 切片供 l1 从项目打开/界面删除；C4 套件不带这些参数）（真实 Flask + 内嵌 PG +
AUTH_ENABLED=True + 假 COS_BUCKET/COS_REGION/SECRET + 产品上限 900,000,000 → capability
available）。ingestion 控制 API 与 COS 分块 PUT 由 page.route 的**有状态假后端**承担
（R1 lib 为 C4 的字节保留版：PUT 体按分块缓冲，场景重组后与 OPFS 产物 sha256 逐字节
比对；另有 gateCreate 闸给出「ingestion 建立时任务已 ready」的顺序证明；popup 交接
场景的路由注册在 context 上）。登录：真实 `/login` 表单。

夹具：`bf-580x300.kfb` / `fl-600x400.kfbf`（快速）、`bf-2g.kfb`（b2/e3：取消与复制
中刷新，产物不上传）、`bf-12000.kfb`（e1：产物 ~230MB < 产品上限，8 MiB/片分块）。

## 2. C4 回归（上传接入不回退）

    node tests/browser/slide_tools_c4/run_e2e.js               # 15 场景

预期 **15/15**（`results/c4-e2e.txt`）。

    node tests/browser/slide_tools_c4/run_workbench.js \
      --out .gate-tmp/slide-tools-c4/workbench-seq-after-r1.json
    node -e "const b=require('./docs/review-evidence/slide-tools/C4/results/workbench-seq-before.json'),a=require('./.gate-tmp/slide-tools-c4/workbench-seq-after-r1.json');console.log('workbench-seq-equal:',JSON.stringify(b.sequence)===JSON.stringify(a.sequence))"

预期 `workbench-seq-equal: true`（14 请求逐条一致；`results/workbench-seq-compare.txt`）。

## 3. C3 回归（仅转换并保存：隐私/离线不回退）

    node tests/browser/slide_tools_c3/run_e2e.js               # 18 条记录

预期 **18/18**、末行 E2E ALL PASS（`results/c3-e2e.{txt,json}`；其中
j-network-capture 零 `/api/`、k-offline-full-flow 离线转换+保存 sha==原生）。

## 4. vitest（前端单元 + i18n 契约）

    TMPDIR=$PWD/.gate-tmp npx vitest run tests/js

预期 **Test Files 38 passed (38) / Tests 607 passed (607)**
（`results/vitest.txt`；新增 `tests/js/r1-convert-upload.test.ts` 33 条——工作台
KFB 入口/零请求、弹窗拦截回退、popup 交接消息形状、i18n zh/en 键契约）。
注意：不要用裸 `npx vitest run`——会收集 `tests/e2e/*.spec.ts`（Playwright 用例，
需 Playwright runner 与运行中的 e2e 服务）与 artifacts 复现脚本，属预期失败。

## 5. pytest（服务端门禁）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_slide_tools_upload_capability.py \
      tests/test_r1_conversion_gate.py \
      tests/test_ingestion_api.py -p no:cacheprovider -q

预期 **37 passed**（`results/pytest.txt`；含
`test_capability_endpoint_exposes_account`（二轮起另断言 `account_label`）；加上
`tests/test_slide_tools_page.py` 共 50 passed。分跑亦过：
capability 单文件 10 passed；gate+ingestion 27 passed）。

全量门禁（审查修复后，串行、不与浏览器测试并行）：

    TMPDIR=$PWD/.gate-tmp COLUMNS=200 .venv/bin/python -m pytest tests -p no:cacheprovider -q -x \
      --deselect tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present

预期 **2868 passed, 8 skipped, 1 deselected**（约 27 min；`results/pytest-full.txt`）。
结束时内嵌 PG 可能打印 `pg_ctl: server does not shut down … killing it`，属 teardown
噪声，退出码 0。

## 6. C2 门禁（无整文件物化）

    node tests/browser/slide_tools_c2/test_no_whole_file.js

预期 PASS（`results/c2-no-whole-file.txt`；grep 面含新模块
`static/tools/tools-slides-convert-upload.js`——该模块不经手文件字节，
转换经 runner、上传经 C4 控制器）。

## 7. 已知无关失败（他人未提交改动，不在上列命令中）

`tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`
断言 admin 插件 `pluginVersion == "0.4.12"`，工作区中该 manifest 已被他人升到
0.4.13（`plugins/pathtogether-admin/manifest.json` 属他人文件，本次不动）——与 C4
RERUN 记录一致。
