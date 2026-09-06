#!/usr/bin/env bash
# image-transport-upgrade 回滚（§10.4）：恢复旧镜像+旧前端资源为同一兼容组合。
# 用法：rollback.sh svs-viewer-demo:rollback-<TS>
set -euo pipefail
REMOTE=homePC
CONTAINER=svs-viewer-demo
TAG="${1:?用法: rollback.sh svs-viewer-demo:rollback-<TS>}"

echo "[1/5] 恢复回滚镜像容器（不动 uploads/share/sidecar-sessions/权限数据，"
echo "      不改 PATHTOGETHER_MULTICHANNEL_ENABLED；AI 会话与检查点零写入）"
ssh "$REMOTE" "podman rm -f $CONTAINER && podman run -d --name $CONTAINER \
  --network host \
  -v ~/svs-viewer-demo-data/uploads:/data/uploads \
  -v ~/svs-viewer-demo-data/share:/data/share \
  -v ~/svs-viewer-demo-data/sidecar-sessions:/data/sidecar-sessions \
  --restart unless-stopped \
  -e PORT=18080 \
  --env-file ~/svs-viewer-demo-data/admin.env \
  $TAG"

echo "[2/5] 冒烟：healthz"
ssh "$REMOTE" "curl -fsS http://127.0.0.1:18080/healthz >/dev/null && echo healthz=OK"

echo "[3/5] 本次新增 viewer 派生缓存是进程内存（LRU），容器重启即清；"
echo "      不清 AI/checkpoint 缓存，无全局目录删除。"

echo "[4/5] 验证回滚后页面只发旧协议请求（重载旧入口，tile URL 无 profile/dv query）："
echo "      打开 / → 打开切片 → 开发者工具 Network 过滤 _files/，确认 URL 无 ?profile=；"
echo "      禁止让旧服务端无限刷新（新 dv URL 会被旧端忽略 query 而正常 200，如演练 §6）。"

echo "[5/5] 回滚冒烟三件套：RGB 显示 / 分享权限 / 旧 AI session 恢复（finished 会话可打开）。"
echo "记录：回滚镜像 TAG、时间、结果。再次上线必须用新编码身份（不得复用错误配置的 dv）。"
