# C5-B 复跑指南（百度导入插件）

日期：2026-09-29。全部命令在仓库根 `PathTogether/` 执行；
`TMPDIR=$PWD/.gate-tmp`（/tmp 是小 tmpfs）。证据 JSON/文本在本目录
`results/`。报告：`docs/slide-tools/c5-baidu-plugin-report.md`。

前置：

- `.venv` 可用（依赖零新增——worker 只用 requests/flask 等仓内既有库；
  fake 源/stub 平台纯 stdlib + requests）。
- 原生 CLI 已构建（缺失时 `PATH=$HOME/.cargo/bin:$PATH bash
  scripts/build_slide_transform.sh`）；缺失时依赖 CLI 的用例按既有仓库
  惯例 skip，不算 PASS。
- 平台子代理的 C5 改动在工作树（集成测试需要 app.py 的
  `/api/plugin/v1/imports/*`、桥与 `slide:import` 枚举）。

## 1. 插件本地套件（stub 平台；无 PG、无网络）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      plugins/pathtogether-baidu-import/tests -q

预期（2026-09-29 验收修复后实跑）：**83 passed**（`results/pytest-plugin.txt`）。
分文件：test_platform_client 31 / test_source_and_convert 15 /
test_journal_manifest 8 / test_pipeline 28 / test_worker_main 1。

## 2. 真实 app 集成（PG）

    TMPDIR=$PWD/.gate-tmp .venv/bin/python -m pytest \
      tests/test_baidu_import_plugin_integration.py -q

预期：**3 passed**（`results/pytest-integration.txt`）——全链路（claim →
begin/scratch/topup/write/commit → published → cleanup-confirm → report；
slide ready + 项目关联 + final consumed/scratch released 的 SQL 断言）与
租约 fencing（真实 claim_batch 重领 → 安静放弃）、失败 → retry_items →
新尝试发布到同一 slide_id（批次 failed → succeeded）。tests/conftest.py 自起
内嵌 PG；勿与其它起服务的套件并行。

## 3. manifest 与来源策略 pin

    sha256sum plugins/pathtogether-baidu-import/manifest.json
    # → 7c6ea101584b09670b95c8096369df7021b34812fd5fbc888d15f25d5167fa20

    .venv/bin/python - <<'PY'
import json, sys; sys.path.insert(0, ".")
from plugins.sdk.manifest import validate_manifest
print(validate_manifest(
    json.load(open("plugins/pathtogether-baidu-import/manifest.json"))))  # → []
PY

pin 未加入 `plugins/source-policy.json`（他人在途文件）——安装前管理员
需按报告 §7 加入。证据：`results/manifest-pin.txt`。

## 4. 手动冒烟（可选；stub 换成真平台时）

    PYTHONPATH=plugins/pathtogether-baidu-import \
    PT_PLATFORM_URL=http://127.0.0.1:8000 \
    PT_INSTALLATION_ID=<安装行> PT_INSTALLATION_SECRET=<凭据> \
    SHARE_DATA_DIR=<平台共享目录> \
    SLIDE_TRANSFORM_BIN=slide-transform-core/target/release/slide-transform \
    python3 -m worker          # /healthz 在 127.0.0.1:8062

测试专用 fake 源装配：`BAIDU_SOURCE=fake` +
`PT_FAKE_SHARE_TREE_FILE=<json>`（条目 `{path, fs_id, size, content_b64}`；
生产绝不设置）。真实百度账号属外部门禁：替身成功不宣称真实可用（§9.1）。
