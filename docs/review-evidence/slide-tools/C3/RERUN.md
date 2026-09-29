# C3 复跑命令（reviewer 独立复现）

代码根：`PathTogether/`（branch `slide-id-refactor`）。所有命令在仓库根执行。
浏览器：Playwright Chromium（`node_modules/playwright` 1.62.1，无新下载）。
大文件全部走 `TMPDIR=$PWD/.gate-tmp`（/tmp 是 tmpfs，勿用）；pytest 不与其他重任务并行。
真实样本：`/home/solarise/ZCodeProject/histopilot-suite/切片文件夹/`（证据只用别名 KFB-1 与哈希，
文件名/字节绝不写进任何仓内文件）。

## 0. 前置（产物已在仓）

`static/tools/slide-transform/`（C2 产物，未改动）。如需重建：
`PATH=$HOME/.cargo/bin:$PATH bash scripts/build_slide_transform.sh`（本阶段未重构建）。

## 1. pytest（路由 / CSP / 内联 / i18n 成对 / 主页入口 / wasm MIME）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest tests/test_slide_tools_page.py -q

期望：`10 passed`（记录：`results/pytest.txt`）。

既有主页/i18n 回归（含 `html.count("<script") == 4` 锁定）：

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest tests/test_phase1_auth_ui.py tests/test_slide_id_review_r16_homepage.py -q

期望：`55 passed`。

## 2. 浏览器 e2e（真实 Flask app，场景 a–m）

    node tests/browser/slide_tools_c3/run_e2e.js            # 全部（起内嵌 PG + Flask :8943）
    node tests/browser/slide_tools_c3/run_e2e.js --only d   # 单场景

期望末行 `E2E ALL PASS`（16/16 记录）；明细 `.gate-tmp/slide-tools-c3/e2e/results.json`
（存档 `results/e2e.json`）。首次运行会生成夹具（含 2 GiB 合成 KFB 与 5.2 GiB 稀疏 KFB
占位），全流程约 3–5 分钟。每场景独立浏览器 profile。
说明：`showSaveFilePicker` 以写 OPFS 的替身验证导出字节路径（真实系统选择器 = 外部门禁）；
场景 (k) 用 `context.setOffline(true)`；场景 (j) 断言仅同源 GET 且无文件名/哈希外传。

## 3. 真实样本 KFB-1（走工具页全流程）

    node tests/browser/slide_tools_c3/run_real_sample.js --samples \
      /home/solarise/ZCodeProject/histopilot-suite/切片文件夹

期望末行 `REAL SAMPLE PASS`：页内保存字节 sha256 = 原生 CLI = `385a59c6…`
（`results/real-sample.json`；仓内只有别名与哈希）。

## 3b. >4.9 GiB 全量页面流程（验收方补跑）

    CLI=slide-transform-core/target/release/slide-transform
    IN=.gate-tmp/slide-tools-c2/browser/memfix/bf-10g.kfb   # 缺失则：$CLI gen-kfb $IN --width 81000 --height 81000
    $CLI convert $IN .gate-tmp/big-native.tif --overwrite && NS=$(sha256sum .gate-tmp/big-native.tif | cut -d' ' -f1) && rm .gate-tmp/big-native.tif
    node tests/browser/slide_tools_c3/run_large_page.js --input $IN --native-sha $NS

期望 `LARGE PAGE PASS`（新 profile 必出 uncertain 对话框；OPFS 峰值约 20 GiB；约 5 分钟）。

## 4. C2 运行器回归（本阶段未改 runner/engine；必须仍绿）

    node tests/browser/slide_tools_c2/run_smoke.js     # 期望 SMOKE PASS
    node tests/browser/slide_tools_c2/run_faults.js    # 期望 FAULT MATRIX: 26/26 passed

记录：`results/c2-regression.txt`（2026-09-29 实跑输出）。

## 5. 手工核对（CSP / MIME，真实 Flask）

    (.venv/bin/python3 tests/browser/slide_tools_c3/server.py --port 8943 &)
    curl -s -D- -o /dev/null http://127.0.0.1:8943/tools/slides | grep -i content-security
    curl -s -D- -o /dev/null http://127.0.0.1:8943/static/tools/slide-transform/slide_transform_bg.wasm | grep -i content-type
    # 未登录（AUTH_ENABLED=True）：/tools/slides 200；/app 302 /login
    # kill %1

期望：CSP 与 app.py `_SLIDE_TOOLS_CSP` 逐 token 一致；wasm 为 `application/wasm`。
（同一断言在 e2e `csp-real-app` 记录 + pytest `test_wasm_served_as_application_wasm`。）


## 6. 截图（视觉审查）

`.gate-tmp/slide-tools-c3/screens/`：`empty / copying / probe-summary / uncertain-dialog /
converting / ready-not-saved / job-list-resume / en-locale`（PNG，fullPage）。
复跑 e2e 自动重新生成。

## 7. 大文件清理

跑完（或复核完）后释放 `.gate-tmp/slide-tools-c3/` 的多 GB 中间物（保留 `screens/` 与
`e2e/`、`real-sample/` JSON 可选）：

    rm -rf .gate-tmp/slide-tools-c3/profiles .gate-tmp/slide-tools-c3/fixtures \
           .gate-tmp/slide-tools-c3/probe .gate-tmp/slide-tools-c3/*.log
