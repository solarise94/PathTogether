# C4 复跑指南（上传接入）

日期：2026-09-29。全部命令在仓库根 `PathTogether/` 执行；`TMPDIR=$PWD/.gate-tmp`
（/tmp 是小 tmpfs）。证据 JSON 在本目录 `results/`（`c4-1-e2e.json` 是 C4 item 1
的既有产物，未改动）。报告：`docs/slide-tools/c4-upload-report.md`。

前置：原生 CLI 已构建（缺失时 `PATH=$HOME/.cargo/bin:$PATH bash
scripts/build_slide_transform.sh`）；`.venv` 可用；node ≥20（仓内 node_modules 含
vitest/playwright，无新下载）。

## 1. pytest（服务端门禁）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_slide_tools_page.py tests/test_slide_tools_upload_capability.py \
      tests/test_ingestion_api.py tests/test_cos_ingestion_kinds.py \
      tests/test_cos_review_fixes.py tests/test_phase1_auth_ui.py \
      tests/test_slide_id_review_r16_homepage.py -q

预期（2026-09-29 验收复跑）：**112 passed**（`results/pytest.txt`；验收新增 format 常量与核心报告绑定用例）。

- 读取器证明（`test_slide_tools_upload_capability.py`）：原生 CLI 合成夹具 →
  `slide_io.open_slide` 实开 + read_region（荧光另 read_region_channels）；
  `test_viewable_formats_bound_to_proof` 要求证明集合 ==
  `app.SLIDE_TOOLS_VIEWABLE_OUTPUT_FORMATS`。
- CSP：无 COS 配置 = C3 原串；配置后 == 带 COS origin 的串（逐 token）；
  非法桶/区域 fail-closed 矩阵。
- 已知无关失败（他人未提交改动，不在上列命令中）：
  `tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present`
  断言 admin 插件 `pluginVersion == "0.4.12"`，工作区中该 manifest 已被他人升到
  0.4.13（`plugins/pathtogether-admin/manifest.json` 属他人文件，本次不动）。

浏览器测试与全量 pytest 不要并行（本目录命令彼此独立起服务/浏览器）。

## 2. vitest

    TMPDIR=$PWD/.gate-tmp npx vitest run tests/js

- 基线（C4 改动**前**，`results/vitest-baseline.txt`）：
  `Test Files 36 passed (36)` / `Tests 548 passed (548)`。
- 之后（`results/vitest.txt`，复审修复后复跑）：`Test Files 37 passed (37)` / `Tests 562 passed (562)`
  （子代理 555 + 验收新增 2 + 复审 P1/P2 新增 5）。
- 既有断言**零改动**；唯一 harness 改动是 `tests/js/cos-upload.test.ts` 的
  `loadApp` 先同 realm 执行 `static/upload/cos-uploader.js`（加载方式）。
- 新增共享引擎单测：`tests/js/cos-uploader-shared.test.ts`（14 tests，注入假
  apiFetch/storage/source；复审 P1/P2 的 5 条已验证在 f69cc58 的引擎上失败）。

## 3. C4 浏览器 e2e（工具页上传）

    node tests/browser/slide_tools_c4/run_e2e.js            # 全部 15 场景
    node tests/browser/slide_tools_c4/run_e2e.js --only a-bf   # 单场景

预期：逐行 `PASS [a-bf] … PASS [m2-cancel-during-backoff]`，末行 **E2E ALL PASS**；
证据 `.gate-tmp/slide-tools-c4/e2e/results.json`（本目录 `results/c4-e2e.json`）。

被测应用：`tests/browser/slide_tools_c4/server.py`（真实 Flask + 内嵌 PG +
AUTH_ENABLED=True + **假** COS_BUCKET/COS_REGION/SECRET + COS_UPLOAD_CAPABILITY=on
+ 产品上限 900,000,000 < 池准入 9.9e9 → capability available）。每次运行默认自起
服务（约 30–60 s）并重写 `.gate-tmp/slide-tools-c4/creds.json`（一次性随机密码）；
`--reuse-server` 才复用已在同端口运行的实例。
ingestion 控制 API 与 COS 分块 PUT 由 Playwright `page.route` 有状态假后端承担
（路由晚于 CSP 检查——CSP 写错 PUT 会被浏览器拦截，测试失败）。

登录：真实 `/login` 表单（`#login-dialog-username/password` + CSRF 隐藏域），
凭据来自 creds 文件（owner/user 各一；场景默认 user）。

## 4. 工作台原生直传回归（请求序列 before/after）

    # “before” = 重构前 app.js/index.html（验收时由审查方把 HEAD 版本临时放回
    #   工作区独立重捕获，入库为 results/workbench-seq-before.json）；重构后：
    node tests/browser/slide_tools_c4/run_workbench.js \
      --out .gate-tmp/slide-tools-c4/workbench-seq-after.json

预期：14 个上传相关请求（POST /api/ingestions → GET → POST sign
part_numbers=[1..8] → 8×PUT(COS) → POST upload-complete → GET → GET），与
`results/workbench-seq-before.json` 逐条一致（method/path/partNumber/
partNumbers），`pt.cos.jobs` 收口为 `[]`。比较：

    node -e "const b=require('./docs/review-evidence/slide-tools/C4/results/workbench-seq-before.json'),a=require('./docs/review-evidence/slide-tools/C4/results/workbench-seq-after.json');console.log(JSON.stringify(b.sequence)===JSON.stringify(a.sequence))"

预期输出 `true`。

## 5. C3 / C2 回归

    node tests/browser/slide_tools_c3/run_e2e.js     # 18/18（含 n1/n2）
    node tests/browser/slide_tools_c2/run_faults.js  # 26/26
    node tests/browser/slide_tools_c2/run_smoke.js   # SMOKE PASS
    node tests/browser/slide_tools_c2/test_no_whole_file.js  # PASS

证据：`results/c3-regression.json`（顶层 `pass:true`、18/18）、
`results/c2-fault-regression.json`（`passed:26, failed:0`）。

## 6. 结果文件清单（results/）

| 文件 | 内容 |
|---|---|
| `vitest-baseline.txt` | 改动前基线（36/548） |
| `pytest.txt` | 最终 pytest 输出（112 passed） |
| `vitest.txt` | 最终 vitest 输出（37 文件/562） |
| `c4-e2e.json` | C4 工具页上传 e2e 15 场景（含验收新增 j/k 与复审新增 l 记录写入失败、m1/m2 等待中取消） |
| `workbench-seq-before.json` / `-after.json` | 工作台请求序列（重构前/后） |
| `c3-regression.json` | C3 套件 18/18 |
| `c2-fault-regression.json` | C2 故障矩阵 26/26 |
| `c4-1-e2e.json` | （C4 item 1 既有产物，未改动） |
