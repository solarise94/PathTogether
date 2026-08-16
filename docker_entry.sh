#!/bin/sh
# =========================================================================== #
# 容器入口：进程拓扑由 ROLE 环境变量决定（Stage 4-3 独立容器形态）。
#
# ROLE 三态（缺省 all，即历史双进程，demo 现状不变）：
#   all      （缺省）同容器起 gunicorn（Flask 管理端）+ Node AI sidecar。
#   platform 只起 gunicorn（不做 sidecar 探活/启动；仍做 PG / import 预检）。
#   sidecar  只起 sidecar（跳过 Flask/PG 预检；启动前等 AI_FLASK_URL /login
#            可达，最多 30s，超时退出——启动顺序兜底）。
#
# 进程拓扑（all 模式）：
#   - sidecar（node /app/sidecar/dist/index.js）：默认监听 127.0.0.1:8055
#     （AI_SIDECAR_HOST，切勿在 --network host 下改成 0.0.0.0），
#     通过 /internal/ai/* 回调 Flask（127.0.0.1:$PORT，AI_FLASK_URL 推导）读图/落标注/取变更。
#     入站 /run|/sessions 等与回调共用 AI_INTERNAL_TOKEN（X-AI-Internal-Token）。
#   - gunicorn（app:app）：监听 0.0.0.0:$PORT（默认 8000），对外服务管理端，并把
#     /api/ai/* 代理到 sidecar。
#
# 启动顺序（all）：先起 sidecar，轮询 /healthz 直到就绪（最多 30s），再起 gunicorn。
# 进程管理（all）：
#   - 任一子进程退出 → 容器退出（exit code 取首个退出进程的码）。
#   - SIGTERM/SIGINT：先停 gunicorn（优雅 drain），再停 sidecar，最后退出。
# 不依赖 bash 的 wait -n（python:3.12-slim 默认 sh 是 dash，不支持 -n），
# 用 kill -0 轮询监控子进程，纯 POSIX sh 可移植。
# =========================================================================== #
set -u

# --------------------------------------------------------------------------- #
# ROLE 解析（归一化小写；非法值 fallthrough 到 all）
# --------------------------------------------------------------------------- #
_ROLE="$(printf '%s' "${ROLE:-all}" | tr '[:upper:]' '[:lower:]')"
case "$_ROLE" in
  platform|sidecar|all) ;;
  *) echo "[entry] 未知 ROLE='${ROLE:-}'; 回退 all（同容器双进程）" >&2; _ROLE=all ;;
esac

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
# ROLE=sidecar 不在此生成新 token（会与平台容器分叉）；双容器请显式注入
# AI_INTERNAL_TOKEN，或挂载平台 SHARE_DATA_DIR 以读同一文件。
# --------------------------------------------------------------------------- #
SHARE_DATA_DIR="${SHARE_DATA_DIR:-/data/share}"
if [ "$_ROLE" != "sidecar" ] && [ -z "${AI_INTERNAL_TOKEN:-}" ]; then
    TOKEN_FILE="$SHARE_DATA_DIR/ai_internal.token"
    if [ ! -f "$TOKEN_FILE" ]; then
        mkdir -p "$SHARE_DATA_DIR" 2>/dev/null || true
        # python3 生成 32 字节 hex（容器内必有 python3；进程替换避免落盘中间态）。
        python3 -c 'import secrets; print(secrets.token_hex(32))' > "$TOKEN_FILE" \
            && chmod 600 "$TOKEN_FILE" 2>/dev/null || true
        echo "[entry] generated ai_internal.token" >&2
    fi
fi
# 把文件里的 token 导出到 env，使同容器 sidecar 入站鉴权与 Flask 代理头一致。
if [ -z "${AI_INTERNAL_TOKEN:-}" ] && [ -f "$SHARE_DATA_DIR/ai_internal.token" ]; then
    AI_INTERNAL_TOKEN="$(tr -d '\n' < "$SHARE_DATA_DIR/ai_internal.token")"
    export AI_INTERNAL_TOKEN
fi

# --------------------------------------------------------------------------- #
# 0b) PostgreSQL schema 预检（Stage 3b-3）。
# STORAGE_BACKEND ∈ {postgres, dual} 时，启动服务前先 ensure_schema（与 app.py
# import 期的 fail-fast 双保险：这里在 sidecar 起来之前给出更清晰的中文错误）。
# 失败直接退出，绝不带病拉起 gunicorn。json 后端（默认）跳过。
# 仅 ROLE ∈ {all, platform} 执行：sidecar 角色只跑 sidecar，PG 属平台侧。
# --------------------------------------------------------------------------- #
if [ "$_ROLE" != "sidecar" ]; then
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
fi

# --------------------------------------------------------------------------- #
# 0c) sidecar 专属 session 目录（Stage 4-3 session DB 分离）。
# 同容器（ROLE=all）显式 export AI_SESSIONS_DIR=/data/sidecar-sessions，与平台
# SHARE_DATA_DIR 分开。含一次性迁移：新目录为空且旧目录（SHARE_DATA_DIR/ai_sessions）
# 存在时把旧内容 mv 进新目录（仅 once；注释即说明）。独立 sidecar 容器不在此设
# （用户自配 AI_SESSIONS_DIR 指向 sidecar 卷）。
# --------------------------------------------------------------------------- #
if [ "$_ROLE" != "sidecar" ]; then
  # 平台/同容器都准备 sidecar-sessions 目录（同容器时 sidecar 也用它）。
  _SIDECAR_SESSIONS="${AI_SESSIONS_DIR:-/data/sidecar-sessions}"
  export AI_SESSIONS_DIR="$_SIDECAR_SESSIONS"
  mkdir -p "$_SIDECAR_SESSIONS" 2>/dev/null || true
  # 一次性迁移：新目录空 + 旧目录存在 → 把旧目录内容 mv 进新目录。
  # 旧目录 = 平台卷内 ai_sessions（Stage 4-3 前 sidecar 与平台同卷共用的会话目录）。
  _LEGACY_SESSIONS="$SHARE_DATA_DIR/ai_sessions"
  if [ -d "$_LEGACY_SESSIONS" ] && [ -z "$(find "$_SIDECAR_SESSIONS" -mindepth 1 -maxdepth 1 2>/dev/null)" ]; then
    _moved=0
    for _f in "$_LEGACY_SESSIONS"/*; do
      [ -e "$_f" ] || break
      if mv "$_f" "$_SIDECAR_SESSIONS/" 2>/dev/null; then _moved=1; fi
    done
    if [ "$_moved" -eq 1 ]; then
      rmdir "$_LEGACY_SESSIONS" 2>/dev/null || true
      echo "[entry] 迁移旧 sessions（$SHARE_DATA_DIR/ai_sessions → $_SIDECAR_SESSIONS）" >&2
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# 1) 起 sidecar（后台）—— 仅 ROLE ∈ {all, sidecar}。
#
# all 模式：Stage 4-1b 启动顺序 —— sidecar 在启动时一次性解析插件凭证文件
# （SHARE_DATA_DIR/plugin-secret-histopilot.txt，Flask import 期幂等引导写入）。
# 若 sidecar 先于 Flask 首启完成引导，会因找不到文件而永久回退 legacy 适配器。
# 故先做一次轻量 `import app` 预检：完成 owner/插件引导 + PG schema 检查（幂等），
# 保证凭证文件在 sidecar 读取前已存在；失败则拒绝启动（与上面 PG 预检同语义）。
#
# sidecar 模式：Flask/PG 属另一容器，不在此预检；但启动前等 AI_FLASK_URL /login
# 可达（最多 30s，每 0.5s 一次；连接建立即视为可达——平台可能返回 401/302）。
# 超时直接退出，避免 sidecar 以 legacy 适配器误连一个尚未就绪的平台。
# --------------------------------------------------------------------------- #
if [ "$_ROLE" != "platform" ]; then
  if [ "$_ROLE" = "all" ]; then
    if ! _APP_PRECHECK_OUT="$(python3 -c "import app" 2>&1)"; then
      printf '%s\n' "$_APP_PRECHECK_OUT" >&2
      echo "[entry] Flask 应用预检失败（import app 非零退出），拒绝启动" >&2
      exit 1
    fi
  else
    # sidecar 模式：等平台 /login 可达（任何 HTTP 状态即就绪，连接建立即可）。
    _PLATFORM_READY=0
    _j=0
    while [ "$_j" -lt 60 ]; do
      if SVS_PLATFORM_URL="${AI_FLASK_URL%/}/login" node -e '
        const http = require("http");
        const url = new URL(process.env.SVS_PLATFORM_URL);
        const req = http.get(
          { hostname: url.hostname, port: url.port, path: url.pathname, timeout: 1000 },
          (res) => { res.resume(); process.exit(0); }  // 连接建立即就绪
        );
        req.on("error", () => process.exit(1));
        req.on("timeout", () => { req.destroy(); process.exit(1); });
      ' 2>/dev/null; then
        _PLATFORM_READY=1
        break
      fi
      sleep 0.5
      _j=$((_j + 1))
    done
    if [ "$_PLATFORM_READY" -ne 1 ]; then
      echo "[entry] AI_FLASK_URL=$AI_FLASK_URL /login 30s 内不可达，退出（sidecar 依赖平台先就绪）" >&2
      exit 1
    fi
    echo "[entry] platform reachable at $AI_FLASK_URL, starting sidecar" >&2
  fi

  echo "[entry] starting AI sidecar ($SIDECAR_BIN)" >&2
  node "$SIDECAR_BIN" &
  SIDECAR_PID=$!

  # ------------------------------------------------------------------------- #
  # 2) 等 sidecar /healthz 就绪（最多 30s）
  # 用 node 一行做 HTTP 探活（容器内已有 node，无需额外装 curl）。
  # ------------------------------------------------------------------------- #
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
  [ "$_ROLE" = "all" ] && echo "[entry] sidecar ready, starting gunicorn" >&2
fi

# --------------------------------------------------------------------------- #
# 3) 起 gunicorn（后台）—— 仅 ROLE ∈ {all, platform}。
# PORT 尊重环境（demo 多实例并跑不打架；默认 8000 保持生产兼容）。
# --------------------------------------------------------------------------- #
if [ "$_ROLE" != "sidecar" ]; then
  GUNICORN_BIND_PORT="${PORT:-8000}"
  gunicorn app:app \
      -b "0.0.0.0:${GUNICORN_BIND_PORT}" \
      -w "$GUNICORN_WORKERS" \
      --threads "$GUNICORN_THREADS" \
      --access-logfile - --error-logfile - &
  GUNICORN_PID=$!
fi

# --------------------------------------------------------------------------- #
# 4) 监控：任一子进程退出则容器退出。
# 用 kill -0 轮询（dash 不支持 wait -n）；每 0.5s 检查一次。首个退出的子进程
# 的码（wait 取回）作为容器退出码。
# --------------------------------------------------------------------------- #
while [ "$SHUTTING_DOWN" -eq 0 ]; do
    if [ -n "$SIDECAR_PID" ] && ! kill -0 "$SIDECAR_PID" 2>/dev/null; then
        echo "[entry] sidecar exited, shutting down" >&2
        wait "$SIDECAR_PID"
        EXIT_CODE=$?
        break
    fi
    if [ -n "$GUNICORN_PID" ] && ! kill -0 "$GUNICORN_PID" 2>/dev/null; then
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
    [ -n "$GUNICORN_PID" ] && kill -TERM "$GUNICORN_PID" 2>/dev/null
    [ -n "$SIDECAR_PID" ] && kill -TERM "$SIDECAR_PID" 2>/dev/null
    [ -n "$GUNICORN_PID" ] && wait "$GUNICORN_PID" 2>/dev/null
    [ -n "$SIDECAR_PID" ] && wait "$SIDECAR_PID" 2>/dev/null
fi
exit "$EXIT_CODE"
