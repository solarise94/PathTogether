#!/bin/sh
# PathTogether platform entrypoint. AI/navigation services are separate plugins
# and are never started or health-gated by this container.
set -eu

# ---------------------------------------------------------------------------
# 账户引导 footgun 守护（账户系统批次 A，docs §5.1/§10）：
#   - 「REQUIRE_ADMIN_AUTH=1 必须有 bootstrap 秘密」的硬性拒启已移除——
#     数据库已有 owner 时无需 bootstrap 秘密即可启动；空库/无 owner 等状态
#     由应用启动状态机 fail-fast（错误消息更可读，见 app.py §5.2）；
#   - BOOTSTRAP_OWNER_PASSWORD_FILE 被设置但文件不存在/为空/内容为占位符
#     → 拒启（复制 admin.env 未替换）；存在且非占位符则原样传给应用
#     （应用读文件内容，entry 不展开进环境）。
#   - 一版兼容别名 ADMIN_USERNAME / ADMIN_PASSWORD 已随 R3 Wave2-Compat
#     删除：entry 不再检查旧变量，残留配置被直接忽略。
# ---------------------------------------------------------------------------
_BOOTSTRAP_PASSWORD_SENTINEL="<REPLACE_WITH_STRONG_PASSWORD>"
_boot_pw_file="${BOOTSTRAP_OWNER_PASSWORD_FILE:-}"
if [ -n "$_boot_pw_file" ]; then
  if [ ! -f "$_boot_pw_file" ]; then
    echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file does not exist; refusing to start" >&2
    exit 1
  fi
  if [ ! -s "$_boot_pw_file" ]; then
    echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file is empty; refusing to start" >&2
    exit 1
  fi
  # 占位符内容（精确 sentinel / <...> 包裹）= 复制 admin.env 未替换：
  # 与应用层 _is_placeholder_admin_password 同口径，提前拒启（fail-fast）。
  # 命令替换已去除尾部换行；真密码不含 <...> 包裹形态。
  _boot_pw="$(cat "$_boot_pw_file")"
  case "$_boot_pw" in
    "$_BOOTSTRAP_PASSWORD_SENTINEL"|\<*\>)
      echo "[entry] BOOTSTRAP_OWNER_PASSWORD_FILE=$_boot_pw_file contains a placeholder; refusing to start" >&2
      exit 1
      ;;
  esac
fi

_backend="$(printf '%s' "${STORAGE_BACKEND:-json}" | tr '[:upper:]' '[:lower:]')"
case "$_backend" in
  postgres|dual)
    python3 -c '
import pg_store
conn = pg_store.connect()
try:
    pg_store.ensure_schema(conn)
finally:
    conn.close()
'
    ;;
esac

mkdir -p "${UPLOAD_DIR:-/data/uploads}" "${SHARE_DATA_DIR:-/data/share}" "${PLUGIN_BUNDLES_DIR:-/data/plugins}"

# ---------------------------------------------------------------------------
# sample-tma-score 后端（demo 同容器托管）
#
# 监听 127.0.0.1:8061（与 manifest.service.baseUrl 一致），随本容器起停，
# 崩溃 2s 后自动拉起。消掉「重建后忘记 podman exec」的运维点。
# SAMPLE_TMA_BACKEND=0/false/no/off 关闭。生产形态仍应独立容器（P2）；
# 此处只保证 demo 镜像内示例能力在容器生命周期内可用。
# gunicorn 仍 exec 为 PID 1；本后台循环被 reparent 到 PID 1，容器 cgroup
# 回收时一并杀掉。子进程退出由循环自身 wait，不会被 gunicorn waitpid(-1)
# 误当成 worker。
# ---------------------------------------------------------------------------
_sample_tma="$(printf '%s' "${SAMPLE_TMA_BACKEND:-1}" | tr '[:upper:]' '[:lower:]')"
case "$_sample_tma" in
  0|false|no|off)
    echo "[entry] SAMPLE_TMA_BACKEND=$_sample_tma, skip sample-tma-score backend"
    ;;
  *)
    _tma_py="/app/plugins/sample-tma-score/backend/app.py"
    if [ -f "$_tma_py" ]; then
      echo "[entry] starting sample-tma-score backend on 127.0.0.1:${PT_TMA_SCORE_PORT:-8061}"
      (
        while :; do
          python3 "$_tma_py" || true
          echo "[entry] sample-tma-score backend exited, restart in 2s" >&2
          sleep 2
        done
      ) &
    else
      echo "[entry] sample-tma-score backend not in image, skip"
    fi
    ;;
esac

exec gunicorn app:app \
  -b "0.0.0.0:${PORT:-8000}" \
  -w "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-8}"
