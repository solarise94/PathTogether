#!/usr/bin/env bash
# -*- mode: shell-script; coding: utf-8 -*-
"""账户/导入/项目 UI 升级（2026-09-14 spec §8.4）统一验收入口。

一次运行完成：
  1. 后端回归 + 新增用例（固定 17 个文件，--junitxml 留证）；
  2. npm run test:js（Vitest 单元层）；
  3. 真实 Chromium Playwright（admin-workbench / toolbar-account-upgrade /
     import-project-upgrade 三个 spec，workers=1，HTML 报告落产物目录）；
  4. git diff --check + 未跟踪源码文件的行尾空白/冲突标记检查。

规则（spec §8.4）：
  - set -euo pipefail，不用 || true 吞失败；
  - 测试文件缺失、pytest 收集 0 项 → 直接失败退出；
  - Chromium 缺失时先 npx playwright install chromium 再重试一次（重试仍
    失败则失败）；E2E 端口由 playwright.config.ts 的 webServer 选择，CI=1
    不复用未知服务器；
  - 各阶段退出码汇总打印，任一非 0 → 脚本退出 1（fail-closed）。

产物目录：artifacts/import-project-upgrade-<UTC 时间戳>/
  backend.xml            pytest JUnit
  vitest.log             Vitest 输出
  playwright.log         Playwright 输出（含 chromium 安装重试记录）
  playwright-report/     Playwright HTML 报告（+ trace，retain-on-failure）
  env.txt                运行环境快照（用于验收报告「命令结果」栏）
"""
set -euo pipefail

# --------------------------------------------------------------------------- #
# 仓库根 + .venv 前置 PATH（Playwright webServer 的 python3 必须来自 .venv）
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
export PATH="$REPO_ROOT/.venv/bin:$PATH"

ART="$REPO_ROOT/artifacts/import-project-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$ART"

# 汇总表：STAGE_NAMES[i] <-> STAGE_CODES[i]
STAGE_NAMES=()
STAGE_CODES=()
record() { STAGE_NAMES+=("$1"); STAGE_CODES+=("$2"); }
overall=0

{
  echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "repo_root=$REPO_ROOT"
  echo "head=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "python=$(.venv/bin/python --version 2>&1)"
  echo "node=$(node --version 2>&1)"
  echo "npm=$(npm --version 2>&1)"
  echo "artifact_dir=$ART"
} > "$ART/env.txt"

echo "== verify_import_project_upgrade =="
echo "artifact dir: $ART"

# --------------------------------------------------------------------------- #
# 0. 前置：测试文件必须齐（缺失 → 直接失败，spec §8.4）
# --------------------------------------------------------------------------- #
PYTEST_FILES=(
  tests/test_admin_batch_d.py tests/test_email_verify_activation.py
  tests/test_account_balance.py tests/test_user_creation_spend_target.py
  tests/test_format_request.py tests/test_slide_format_registry.py
  tests/test_kfb_upload.py tests/test_kfbf_upload.py
  tests/test_upload_v2.py tests/test_upload_accounting_recovery.py
  tests/test_format_request_concurrency.py tests/test_format_request_migration.py
  tests/test_project_creation_upgrade.py tests/test_conversion_task_api.py
  tests/test_baidu_adapter.py tests/test_baidu_imports.py
  tests/test_baidu_import_recovery.py
)
E2E_SPECS=(
  tests/e2e/admin-workbench.spec.ts
  tests/e2e/toolbar-account-upgrade.spec.ts
  tests/e2e/import-project-upgrade.spec.ts
)
missing=0
for f in "${PYTEST_FILES[@]}" "${E2E_SPECS[@]}" tests/js/project-import-upgrade.test.ts; do
  if [ ! -f "$f" ]; then
    echo "MISSING TEST FILE: $f" >&2
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo "fail-fast: required test files missing (see above)" >&2
  exit 1
fi

# --------------------------------------------------------------------------- #
# 1. pytest：后端回归 + 新增用例（收集 0 项 → 直接失败）
# --------------------------------------------------------------------------- #
echo
echo "== [1/4] pytest backend matrix =="
pytest_code=0
.venv/bin/python -m pytest "${PYTEST_FILES[@]}" -q \
  --junitxml="$ART/backend.xml" 2>&1 | tee "$ART/pytest.log" || pytest_code=${PIPESTATUS[0]}
record "pytest" "$pytest_code"

# JUnit 里收集数必须 > 0（pytest exit 5 之外再显式校验，防 --junitxml 异常）
collected="$(.venv/bin/python - "$ART/backend.xml" <<'PY'
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.argv[1]).getroot()
except Exception:
    print(0); raise SystemExit
total = 0
for el in root.iter("testsuite"):
    try:
        total += int(el.get("tests") or 0)
    except ValueError:
        pass
print(total)
PY
)"
if [ "$collected" -le 0 ]; then
  echo "fail-fast: pytest collected 0 tests (junit total=$collected)" >&2
  exit 1
fi
echo "pytest collected tests (junit): $collected"
if [ "$pytest_code" -ne 0 ]; then overall=1; fi

# --------------------------------------------------------------------------- #
# 2. Vitest 单元层
# --------------------------------------------------------------------------- #
echo
echo "== [2/4] npm run test:js =="
vitest_code=0
npm run test:js 2>&1 | tee "$ART/vitest.log" || vitest_code=${PIPESTATUS[0]}
record "npm run test:js" "$vitest_code"
if [ "$vitest_code" -ne 0 ]; then overall=1; fi

# --------------------------------------------------------------------------- #
# 3. Playwright 真实 Chromium E2E（CI=1：不复用未知 server；HTML 报告入产物目录）
# --------------------------------------------------------------------------- #
echo
echo "== [3/4] playwright e2e (chromium, workers=1) =="
export PLAYWRIGHT_HTML_REPORT="$ART/playwright-report"

run_playwright() {
  CI=1 npx playwright test \
    tests/e2e/admin-workbench.spec.ts \
    tests/e2e/toolbar-account-upgrade.spec.ts \
    tests/e2e/import-project-upgrade.spec.ts \
    --project=chromium --workers=1 --reporter=line,html \
    2>&1 | tee -a "$ART/playwright.log"
}

pw_code=0
run_playwright || pw_code=${PIPESTATUS[0]}
if [ "$pw_code" -ne 0 ] && grep -qE "Executable doesn't exist|playwright install" "$ART/playwright.log"; then
  echo "chromium missing -> npx playwright install chromium, then retry" | tee -a "$ART/playwright.log"
  npx playwright install chromium 2>&1 | tee -a "$ART/playwright.log"
  pw_code=0
  run_playwright || pw_code=${PIPESTATUS[0]}
fi
record "playwright e2e" "$pw_code"
if [ "$pw_code" -ne 0 ]; then overall=1; fi

# --------------------------------------------------------------------------- #
# 4. 干净 diff 检查：git diff --check + 未跟踪源码文件（git 不覆盖它们）
# --------------------------------------------------------------------------- #
echo
echo "== [4/4] whitespace / conflict-marker checks =="
ws_code=0
if ! git diff --check; then
  echo "git diff --check reported problems" >&2
  ws_code=1
fi

# 未跟踪的 py/js/ts/css/html/sql/sh 同样查行尾空白与冲突标记
# （[[:blank:]] = 空格/Tab；注意 grep -E 括号内 "\t" 不转义，会误匹配字母 t
#  与行尾续行反斜杠，故必须用 POSIX 字符类）
untracked_list="$(git ls-files --others --exclude-standard \
  | grep -E '\.(py|js|ts|css|html|sql|sh)$' || true)"
if [ -n "$untracked_list" ]; then
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if grep -nE '[[:blank:]]+$' "$f" >/dev/null; then
      echo "trailing whitespace: $f" >&2
      grep -nE '[[:blank:]]+$' "$f" | head -5 >&2
      ws_code=1
    fi
    if grep -nxE '<{7,}|>{7,}|={7,}' "$f" >/dev/null; then
      echo "conflict marker: $f" >&2
      grep -nxE '<{7,}|>{7,}|={7,}' "$f" | head -5 >&2
      ws_code=1
    fi
  done <<< "$untracked_list"
fi
record "git diff --check + untracked hygiene" "$ws_code"
if [ "$ws_code" -ne 0 ]; then overall=1; fi

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
echo
echo "== summary =="
{
  echo "artifact_dir=$ART"
  for i in "${!STAGE_NAMES[@]}"; do
    printf '%-40s exit=%s\n' "${STAGE_NAMES[$i]}" "${STAGE_CODES[$i]}"
  done
} | tee "$ART/summary.txt"

if [ "$overall" -ne 0 ]; then
  echo "verify_import_project_upgrade: FAIL (see codes above; artifacts in $ART)" >&2
  exit 1
fi
echo "verify_import_project_upgrade: PASS (artifacts in $ART)"
