#!/bin/sh
# PathTogether platform entrypoint. AI/navigation services are separate plugins
# and are never started or health-gated by this container.
set -eu

_ADMIN_PASSWORD_SENTINEL="<REPLACE_WITH_STRONG_PASSWORD>"
_require_auth="$(printf '%s' "${REQUIRE_ADMIN_AUTH:-}" | tr '[:upper:]' '[:lower:]')"
case "$_require_auth" in
  1|true|yes)
    _pw="${ADMIN_PASSWORD:-}"
    case "$_pw" in
      ""|"$_ADMIN_PASSWORD_SENTINEL"|\<*\>)
        echo "[entry] REQUIRE_ADMIN_AUTH=1 but ADMIN_PASSWORD is empty or a placeholder; refusing to start" >&2
        exit 1
        ;;
    esac
    ;;
esac

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

exec gunicorn app:app \
  -b "0.0.0.0:${PORT:-8000}" \
  -w "${GUNICORN_WORKERS:-2}" \
  --threads "${GUNICORN_THREADS:-8}"
