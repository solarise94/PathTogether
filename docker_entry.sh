#!/bin/sh
# =========================================================================== #
# 容器入口：同进程起 Flask 管理端（gunicorn）+ Node AI sidecar。
#
# 进程拓扑：
#   - sidecar（node /app/sidecar/dist/index.js）：仅监听 127.0.0.1:8055，
#     通过 /internal/ai/* 回调 Flask（127.0.0.1:$PORT，AI_FLASK_URL 推导）读图/落标注/取变更。
#   - gunicorn（app:app）：监听 0.0.0.0:$PORT（默认 8000），对外服务管理端，并把
#     /api/ai/* 代理到 sidecar。
#
# 启动顺序：先起 sidecar，轮询 /healthz 直到就绪（最多 30s），再起 gunicorn。
# 进程管理：
#   - 任一子进程退出 → 容器退出（exit code 取首个退出进程的码）。
#   - SIGTERM/SIGINT：先停 gunicorn（优雅 drain），再停 sidecar，最后退出。
# 不依赖 bash 的 wait -n（python:3.12-slim 默认 sh 是 dash，不支持 -n），
# 用 kill -0 轮询监控子进程，纯 POSIX sh 可移植。
# =========================================================================== #
set -u

# --------------------------------------------------------------------------- #
# Demo fail-closed：REQUIRE_ADMIN_AUTH=1 时拒绝空密码、文档精确 sentinel、
# 或 <...> 占位符。sentinel 必须与 app.py ADMIN_PASSWORD_PLACEHOLDER_SENTINEL
# 及 docs/demo-deployment.md 示例一致。内网默认不设该开关（无密码则免登录）。
# --------------------------------------------------------------------------- #
_ADMIN_PASSWORD_SENTINEL="<REPLACE_WITH_STRONG_PASSWORD>"
_require_auth="$(printf '%s' "${REQUIRE_ADMIN_AUTH:-}" | tr '[:upper:]' '[:lower:]')"
case "$_require_auth" in
  1|true|yes)
    _pw="${ADMIN_PASSWORD:-}"
    _placeholder=0
    if [ -z "$_pw" ] || [ "$_pw" = "$_ADMIN_PASSWORD_SENTINEL" ]; then
      _placeholder=1
    else
      case "$_pw" in
        \<*\>) _placeholder=1 ;;
      esac
    fi
    if [ "$_placeholder" -eq 1 ]; then
      echo "[entry] REQUIRE_ADMIN_AUTH=1 but ADMIN_PASSWORD is empty or a placeholder; refusing to start" >&2
      exit 1
    fi
    ;;
esac

SIDECAR_BIN="${SIDECAR_BIN:-/app/sidecar/dist/index.js}"
SIDECAR_URL="${AI_SIDECAR_URL:-http://127.0.0.1:8055}"
# sidecar 回调 Flask 的地址：显式 AI_FLASK_URL 优先，否则按 PORT 推导
# （gunicorn 绑 0.0.0.0:$PORT，sidecar 走 loopback 回调同端口）。不写死 8000，
# 否则 PORT≠8000 的部署（如 demo :18080）回调失败或串到同主机另一实例。
export AI_FLASK_URL="${AI_FLASK_URL:-http://127.0.0.1:${PORT:-8000}}"
# gunicorn 启动参数与原 CMD 一致（-w 2 --threads 8）。
GUNICORN_WORKERS="${GUNICORN_WORKERS:-2}"
GUNICORN_THREADS="${GUNICORN_THREADS:-8}"

# 运行态子进程 PID。
SIDECAR_PID=""
GUNICORN_PID=""

# 退出码：首个退出的子进程码，缺省 0。
EXIT_CODE=0
# 标记：是否已在收尾（避免 cleanup 与监控循环重复 kill）。
SHUTTING_DOWN=0

# --------------------------------------------------------------------------- #
# 信号处理：先停 gunicorn，再停 sidecar。
# gunicorn 收 SIGTERM 会优雅 drain（处理完在途请求再退出）；sidecar 直接 SIGTERM。
# --------------------------------------------------------------------------- #
cleanup() {
    # 重置 trap，避免重入。
    trap '' TERM INT
    SHUTTING_DOWN=1
    echo "[entry] received signal, stopping gunicorn then sidecar" >&2
    if [ -n "$GUNICORN_PID" ]; then
        kill -TERM "$GUNICORN_PID" 2>/dev/null
    fi
    if [ -n "$SIDECAR_PID" ]; then
        kill -TERM "$SIDECAR_PID" 2>/dev/null
    fi
    wait "$GUNICORN_PID" 2>/dev/null
    wait "$SIDECAR_PID" 2>/dev/null
    exit "$EXIT_CODE"
}
trap cleanup TERM INT

# --------------------------------------------------------------------------- #
# 0) 预生成 internal 回调共享 token。
# 启动顺序是 sidecar 先于 Flask，但 token 文件由 Flask 首 boot 才创建——
# sidecar 启动时读不到会直接 ENOENT 退出（鸡生蛋问题）。这里在起 sidecar
# 前确保文件存在；Flask 的 _load_or_create_ai_internal_token 会读到同一文件。
# --------------------------------------------------------------------------- #
SHARE_DATA_DIR="${SHARE_DATA_DIR:-/data/share}"
if [ -z "${AI_INTERNAL_TOKEN:-}" ]; then
    TOKEN_FILE="$SHARE_DATA_DIR/ai_internal.token"
    if [ ! -f "$TOKEN_FILE" ]; then
        mkdir -p "$SHARE_DATA_DIR" 2>/dev/null || true
        # python3 生成 32 字节 hex（容器内必有 python3；进程替换避免落盘中间态）。
        python3 -c 'import secrets; print(secrets.token_hex(32))' > "$TOKEN_FILE" \
            && chmod 600 "$TOKEN_FILE" 2>/dev/null || true
        echo "[entry] generated ai_internal.token" >&2
    fi
fi

# --------------------------------------------------------------------------- #
# 0b) PostgreSQL schema 预检（Stage 3b-3）。
# STORAGE_BACKEND ∈ {postgres, dual} 时，启动服务前先 ensure_schema（与 app.py
# import 期的 fail-fast 双保险：这里在 sidecar 起来之前给出更清晰的中文错误）。
# 失败直接退出，绝不带病拉起 gunicorn。json 后端（默认）跳过。
# --------------------------------------------------------------------------- #
_BACKEND="$(printf '%s' "${STORAGE_BACKEND:-}" | tr '[:upper:]' '[:lower:]')"
case "$_BACKEND" in
  postgres|dual)
    if ! python3 -c '
import sys
import pg_store
try:
    conn = pg_store.connect()
    try:
        pg_store.ensure_schema(conn)
    finally:
        conn.close()
except Exception as exc:
    sys.stderr.write("[entry] PostgreSQL schema 初始化失败，拒绝启动: %s\n" % exc)
    sys.exit(1)
'; then
      exit 1
    fi
    [ "$_BACKEND" = "dual" ] && echo "[entry] STORAGE_BACKEND=dual: expand 形态，读 json 权威、写镜像 pg" >&2
    ;;
esac

# --------------------------------------------------------------------------- #
# 1) 起 sidecar（后台）
# --------------------------------------------------------------------------- #
echo "[entry] starting AI sidecar ($SIDECAR_BIN)" >&2
node "$SIDECAR_BIN" &
SIDECAR_PID=$!

# --------------------------------------------------------------------------- #
# 2) 等 sidecar /healthz 就绪（最多 30s）
# 用 node 一行做 HTTP 探活（容器内已有 node，无需额外装 curl）。
# --------------------------------------------------------------------------- #
HEALTHZ_URL="${SIDECAR_URL%/}/healthz"
READY=0
i=0
while [ "$i" -lt 60 ]; do
    # node 退出码 0 表示 /healthz 返回 200。env 必须作为命令前缀
    # （写在 -e 脚本后面会变成 argv 而非环境变量，探活恒失败）。
    if SVS_HEALTHZ_URL="$HEALTHZ_URL" node -e '
        const http = require("http");
        const url = new URL(process.env.SVS_HEALTHZ_URL);
        const req = http.get(
            { hostname: url.hostname, port: url.port, path: url.pathname, timeout: 1000 },
            (res) => { process.exit(res.statusCode === 200 ? 0 : 1); }
        );
        req.on("error", () => process.exit(1));
        req.on("timeout", () => { req.destroy(); process.exit(1); });
    ' 2>/dev/null; then
        READY=1
        break
    fi
    # sidecar 进程已提前退出 → 不再等待。
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        echo "[entry] sidecar exited before /healthz became ready" >&2
        wait "$SIDECAR_PID"
        EXIT_CODE=$?
        exit "$EXIT_CODE"
    fi
    sleep 0.5
    i=$((i + 1))
done

if [ "$READY" -ne 1 ]; then
    echo "[entry] sidecar /healthz not ready within 30s, aborting" >&2
    kill -TERM "$SIDECAR_PID" 2>/dev/null
    wait "$SIDECAR_PID" 2>/dev/null
    exit 1
fi

echo "[entry] sidecar ready, starting gunicorn" >&2

# --------------------------------------------------------------------------- #
# 3) 起 gunicorn（后台与 sidecar 并行）
# PORT 尊重环境（demo 多实例并跑不打架；默认 8000 保持生产兼容）。
# --------------------------------------------------------------------------- #
GUNICORN_BIND_PORT="${PORT:-8000}"
gunicorn app:app \
    -b "0.0.0.0:${GUNICORN_BIND_PORT}" \
    -w "$GUNICORN_WORKERS" \
    --threads "$GUNICORN_THREADS" \
    --access-logfile - --error-logfile - &
GUNICORN_PID=$!

# --------------------------------------------------------------------------- #
# 4) 监控：任一子进程退出则容器退出。
# 用 kill -0 轮询（dash 不支持 wait -n）；每 0.5s 检查一次。首个退出的子进程
# 的码（wait 取回）作为容器退出码。
# --------------------------------------------------------------------------- #
while [ "$SHUTTING_DOWN" -eq 0 ]; do
    if ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        echo "[entry] sidecar exited, shutting down" >&2
        wait "$SIDECAR_PID"
        EXIT_CODE=$?
        break
    fi
    if ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
        echo "[entry] gunicorn exited, shutting down" >&2
        wait "$GUNICORN_PID"
        EXIT_CODE=$?
        break
    fi
    sleep 0.5
done

# 收尾：把仍在运行的另一个进程停掉，避免孤儿。trap 已在 cleanup 里处理信号路径；
# 这里是正常退出路径（子进程先退），直接 kill + wait。
if [ "$SHUTTING_DOWN" -eq 0 ]; then
    trap '' TERM INT
    kill -TERM "$GUNICORN_PID" 2>/dev/null
    kill -TERM "$SIDECAR_PID" 2>/dev/null
    wait "$GUNICORN_PID" 2>/dev/null
    wait "$SIDECAR_PID" 2>/dev/null
fi
exit "$EXIT_CODE"
