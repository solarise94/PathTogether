#!/usr/bin/env bash
# ===========================================================================
# release-plugin-bundle.sh —— homePC 插件版本化发布脚本（§16.1 发布底座，PR3b）
#
# 形态：本脚本在**操作者的工作站**上执行（宿主机），所有对 homePC 的操作都经
#       `ssh homePC` alias（~/.ssh/config 维护，不硬编码任何公网/LAN IP）；
#       远程 shell 为 homePC 上的 GNU coreutils 环境（mv -T / ln -sfn 可用）。
#       本脚本**不执行** git push、不触碰数据库、不改 production pin 文件
#       （pin 更新是代码变更，走 repo → 镜像重建 → 部署，见 runbook §步骤 10）。
#
# 目录布局（§16.1 第 5 步）：
#   ~/svs-viewer-demo-data/plugins/            # 宿主机插件根（容器 :ro 挂载）
#   ├── releases/<plugin_id>-<version>/        # 不可变版本化 bundle
#   │   ├── manifest.json  ui/ ...
#   ├── <plugin_id> -> releases/<plugin_id>-<version>   # 原子 symlink 入口
#   └── releases/RELEASE_LOG                   # 不含密钥的发布记录
#
# 阶段（一次发布按 preflight → stage → preflight → switch → verify 顺序手工执行；
#       出问题 rollback）：
#   stage <id> <version> [--source <dir>]
#         把本地 bundle rsync 到远程 releases/<id>-<version>/（目标已存在即拒绝，
#         版本化目录不可变；不提供覆盖，发新版请换版本号）。
#   preflight <id> <version> [--pin <sha256>]
#         校验：release 存在；manifest 可解析；hash 与 --pin（缺省读仓库
#         plugins/source-policy.json 的 pin）一致；当前入口 target 不是该 release
#         （不在用）；symlink target 位于插件根内；磁盘余量。
#   switch <id> <version>
#         原子切换入口 symlink（临时链接 + mv -T rename 语义）；切换前把旧
#         target 记入 RELEASE_LOG。
#   verify <id> <version>
#         切换后复核：入口 target 正确且位于根内；manifest hash 与 stage 记录
#         一致；平台 /healthz 200；（admin 插件另查 /admin 宿主页可达，见 runbook
#         三身份验证——脚本只做无凭据探测）。
#   rollback <id>
#         按 RELEASE_LOG 最近一次 switch 记录切回旧 target（再记一条 rollback）。
#
# 发布记录格式（RELEASE_LOG，追加；不含密钥/不含操作机环境细节）：
#   <UTC ISO> <operator> <stage> <plugin_id> <version> <manifest_sha256> \
#   old_target=<...|-> new_target=<...|-> [note=...]
# ===========================================================================
set -euo pipefail

SSH_HOST="${PT_RELEASE_SSH_HOST:-homePC}"
# REMOTE_PLUGINS_ROOT / RELEASES_DIR / RELEASE_LOG 在 die() 定义之后解析（见下）。
HEALTH_URL="${PT_RELEASE_HEALTH_URL:-https://pt.solarise94.fun/healthz}"
MIN_FREE_MB="${PT_RELEASE_MIN_FREE_MB:-512}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_POLICY_FILE="$REPO_ROOT/plugins/source-policy.json"

die() { echo "release: ERROR: $*" >&2; exit 1; }
info() { echo "release: $*"; }

# 远端插件根必须解析为**绝对路径**：远端命令一律单引号包裹路径，字面 `~` 不会
# 展开（存在性检查会静默失真）；本地 macOS 的 $HOME 又与远端不同，故经 ssh
# 取远端 $HOME 拼接。
if [ -n "${PT_RELEASE_PLUGINS_ROOT:-}" ]; then
  REMOTE_PLUGINS_ROOT="$PT_RELEASE_PLUGINS_ROOT"
else
  REMOTE_HOME="$(ssh "$SSH_HOST" 'printf %s "$HOME"')" || die "无法经 ssh 解析远端 HOME"
  [ -n "$REMOTE_HOME" ] || die "远端 HOME 为空"
  REMOTE_PLUGINS_ROOT="$REMOTE_HOME/svs-viewer-demo-data/plugins"
fi
RELEASES_DIR="$REMOTE_PLUGINS_ROOT/releases"
RELEASE_LOG="$RELEASES_DIR/RELEASE_LOG"

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
}

remote() { ssh "$SSH_HOST" "$@"; }

release_dir() {  # <id> <version> -> $RELEASES_DIR/<id>-<version>
  echo "$RELEASES_DIR/$1-$2"
}

manifest_sha256_local() {  # 本地文件 hash（stage 前用）
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    sha256sum "$1" | cut -d' ' -f1
  fi
}

policy_pin() {  # 仓库 source-policy.json 中该插件的 pin（无则空）
  python3 - "$LOCAL_POLICY_FILE" "$1" <<'PY'
import json, sys
try:
    policy = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
pin = policy.get(sys.argv[2])
print(pin if isinstance(pin, str) else "")
PY
}

append_log() {  # <stage> <id> <version> <sha256|-> <old|-> <new|-> [note]
  local ts op stage id ver sha old new note
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  op="${PT_RELEASE_OPERATOR:-${USER:-unknown}}"
  stage="$1"; id="$2"; ver="$3"; sha="$4"; old="$5"; new="$6"
  note="${7:-}"
  remote "mkdir -p '$RELEASES_DIR' && printf '%s %s %s %s %s %s old_target=%s new_target=%s %s\n' \
    '$ts' '$op' '$stage' '$id' '$ver' '$sha' '$old' '$new' '$note' >> '$RELEASE_LOG'"
  info "RELEASE_LOG 已追加（$stage $id ${ver}）"
}

current_target() {  # 入口 symlink 当前 target（相对名，如 releases/x-1.2.0；无入口输出 -）
  remote "t=\$(readlink '$REMOTE_PLUGINS_ROOT/$1' 2>/dev/null || true); echo \"\${t:--}\""
}

cmd_stage() {
  local id="$1" ver="$2" src="${3:-$REPO_ROOT/plugins/$1}"
  local dest; dest="$(release_dir "$id" "$ver")"
  [ -d "$src" ] || die "本地 bundle 不存在：$src"
  [ -f "$src/manifest.json" ] || die "bundle 缺 manifest.json：$src"
  # 版本一致性：manifest.id 必须与目录名一致（防把 A 插件发成 B 的版本目录）
  local mid; mid="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' "$src/manifest.json")"
  [ "$mid" = "$id" ] || die "manifest.id=$mid 与目录名 $id 不一致"
  remote "[ -e '$dest' ]" >/dev/null 2>&1 && die "目标 release 已存在（版本化目录不可变）：$dest"
  local sha; sha="$(manifest_sha256_local "$src/manifest.json")"
  info "stage：rsync $src -> $SSH_HOST:${dest}（manifest sha256=${sha}）"
  rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$src/" "$SSH_HOST:$dest/"
  remote "[ -f '$dest/manifest.json' ]" >/dev/null 2>&1 || die "stage 后远程缺 manifest.json"
  append_log stage "$id" "$ver" "$sha" "-" "releases/$id-$ver" "staged_from_workstation"
}

cmd_preflight() {
  local id="$1" ver="$2" pin="${3:-}"
  local dest; dest="$(release_dir "$id" "$ver")"
  if [ -z "$pin" ]; then
    pin="$(policy_pin "$id")"
    [ -n "$pin" ] || die "未给 --pin 且仓库 source-policy.json 无 $id 的 pin（admin 插件必须显式 pin）"
  fi
  remote "[ -d '$dest' ]" >/dev/null 2>&1 || die "目标 release 不存在：$dest"
  # manifest 可解析 + hash
  remote "python3 -c 'import json,sys;json.load(open(sys.argv[1]))' '$dest/manifest.json'" \
    >/dev/null 2>&1 || die "远程 manifest.json 无法解析：$dest/manifest.json"
  local rsha; rsha="$(remote "sha256sum '$dest/manifest.json' | cut -d' ' -f1")"
  [ "$rsha" = "$pin" ] || die "manifest hash 不匹配：release=$rsha pin=$pin"
  # 不在用
  local cur; cur="$(current_target "$id")"
  [ "$cur" != "releases/$id-$ver" ] || die "该 release 已是当前入口（不在用检查失败）"
  # symlink target 位于插件根内（防既有入口逃逸；无入口跳过）
  if [ "$cur" != "-" ]; then
    local tgtcheck; tgtcheck="$(remote "case '$cur' in releases/*) echo ok;; *) echo bad;; esac")"
    [ "$tgtcheck" = "ok" ] || die "当前入口 target 异常（须为 releases/* 相对路径）：$cur"
  fi
  # 磁盘余量
  local free_mb; free_mb="$(remote "df -Pm '$REMOTE_PLUGINS_ROOT' | awk 'NR==2{print \$4}'")"
  [ "$free_mb" -ge "$MIN_FREE_MB" ] || die "磁盘余量不足：${free_mb}MB < ${MIN_FREE_MB}MB"
  info "preflight 通过：release=$dest hash=$rsha current=$cur free=${free_mb}MB"
}

cmd_switch() {
  local id="$1" ver="$2"
  local dest; dest="$(release_dir "$id" "$ver")"
  remote "[ -d '$dest' ]" >/dev/null 2>&1 || die "目标 release 不存在：$dest"
  local cur; cur="$(current_target "$id")"
  [ "$cur" != "releases/$id-$ver" ] || die "该 release 已是当前入口，无需切换"
  local rsha; rsha="$(remote "sha256sum '$dest/manifest.json' | cut -d' ' -f1")"
  # 原子切换：临时 symlink + mv -T（rename 语义；远程为 GNU coreutils）。
  # 容器以 :ro 挂载插件根，宿主机侧 rename 对容器即原子可见。
  info "switch：$id 入口 $cur -> releases/$id-$ver"
  remote "set -e; cd '$REMOTE_PLUGINS_ROOT'; ln -sfn 'releases/$id-$ver' '.$id.switch.tmp'; mv -T '.$id.switch.tmp' '$id'"
  append_log switch "$id" "$ver" "$rsha" "$cur" "releases/$id-$ver"
}

cmd_verify() {
  local id="$1" ver="$2"
  local dest; dest="$(release_dir "$id" "$ver")"
  local cur; cur="$(current_target "$id")"
  [ "$cur" = "releases/$id-$ver" ] || die "入口 target 不是新 release：$cur"
  # target 解析后必须位于插件根内（§16.1：加载器同款校验，防链接逃逸；~ 在
  # 远程展开后比对，避免本地把 ~ 当字面量）
  remote "r=\$(realpath '$REMOTE_PLUGINS_ROOT/$id'); case \"\$r\" in \$(realpath '$REMOTE_PLUGINS_ROOT')/*) echo ok;; *) echo bad;; esac" \
    | grep -qx ok || die "入口 symlink 解析后不在插件根内"
  local rsha; rsha="$(remote "sha256sum '$dest/manifest.json' | cut -d' ' -f1")"
  local logged; logged="$(remote "grep ' switch $id $ver ' '$RELEASE_LOG' | tail -1" | awk '{print $6}')"
  [ "$rsha" = "$logged" ] || die "hash 与发布记录不一致：now=$rsha logged=$logged"
  # 平台存活（无凭据探测；admin 三身份验证在 runbook 人工执行）
  curl -fsS --max-time 15 "$HEALTH_URL" >/dev/null || die "平台 healthz 探测失败：$HEALTH_URL"
  info "verify 通过：target=$cur hash=$rsha healthz=ok"
}

cmd_rollback() {
  local id="$1"
  # 取最近一次 switch **或** rollback 记录（rollback 本身也会追加记录——再次
  # rollback 即在其基础上翻回；如需重新上线新版本请走 switch，别叠 rollback）
  local last; last="$(remote "grep -E ' (switch|rollback) $id ' '$RELEASE_LOG' | tail -1" || true)"
  [ -n "$last" ] || die "RELEASE_LOG 无 $id 的 switch/rollback 记录，无法回滚"
  local old_target; old_target="$(echo "$last" | sed -n 's/.*old_target=\([^ ]*\).*/\1/p')"
  local ver; ver="$(echo "$last" | awk '{print $5}')"
  local sha; sha="$(echo "$last" | awk '{print $6}')"
  [ "$old_target" != "-" ] || die "最近一次 switch 无旧 target（首次发布），回滚 = 移除入口，请人工确认"
  remote "[ -d '$REMOTE_PLUGINS_ROOT/$old_target' ]" >/dev/null 2>&1 \
    || die "旧 release 目录不存在：${old_target}（不得指向已删除目录）"
  local cur; cur="$(current_target "$id")"
  info "rollback：$id 入口 $cur -> $old_target"
  remote "set -e; cd '$REMOTE_PLUGINS_ROOT'; ln -sfn '$old_target' '.$id.rollback.tmp'; mv -T '.$id.rollback.tmp' '$id'"
  append_log rollback "$id" "$ver" "$sha" "$cur" "$old_target"
  info "rollback 完成；请复查 /healthz 与 /admin（runbook 步骤 11）"
}

main() {
  [ $# -ge 2 ] || usage
  local stage="$1" id="$2"; shift 2
  case "$stage" in
    stage)
      local ver="${1:-}"; [ -n "$ver" ] || usage; shift
      local src=""
      if [ "${1:-}" = "--source" ]; then src="$2"; fi
      cmd_stage "$id" "$ver" "$src"
      ;;
    preflight)
      local ver="${1:-}"; [ -n "$ver" ] || usage; shift
      local pin=""
      if [ "${1:-}" = "--pin" ]; then pin="$2"; fi
      cmd_preflight "$id" "$ver" "$pin"
      ;;
    switch)    cmd_switch "$id" "${1:?需要 version}";;
    verify)    cmd_verify "$id" "${1:?需要 version}";;
    rollback)  cmd_rollback "$id";;
    *) usage;;
  esac
}

main "$@"
