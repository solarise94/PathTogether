#!/usr/bin/env bash
# image-transport-upgrade 部署（目标：homePC Demo，拓扑依据 docs/demo-deployment.md 2026-08-31 更正版）
# 前置：本脚本仅在获得明确发布授权后由主代理执行；REQUIRE_ADMIN_AUTH=1 沿用原值。
set -euo pipefail

SHA_EXPECTED="${1:?用法: deploy.sh <发布SHA>  ——SHA 见 RUNBOOK.md 发布记录}"
[ -n "$SHA_EXPECTED" ] || { echo "缺少发布 SHA"; exit 1; }
REMOTE=homePC
CODE_DIR=~/svs-viewer-demo
IMAGE=svs-viewer-demo:latest
ROLLBACK_TAG=svs-viewer-demo:rollback-$(date -u +%Y%m%dT%H%M%SZ)
CONTAINER=svs-viewer-demo
AUTH_ENV=~/svs-viewer-demo-data/admin.env

echo "[1/7] 校验本地发布 SHA"
SHA_LOCAL=$(git -C /Users/solarise/ZCodeProject/histopilot-suite/PathTogether rev-parse HEAD)
[ "$SHA_LOCAL" = "$SHA_EXPECTED" ] || { echo "SHA 不符：$SHA_LOCAL != $SHA_EXPECTED"; exit 1; }

echo "[2/7] 保存当前镜像为回滚标签：$ROLLBACK_TAG"
ssh "$REMOTE" "podman tag $IMAGE $ROLLBACK_TAG && podman inspect $IMAGE --format '{{.Digest}}' | tee ~/svs-viewer-demo-data/last-image-digest.txt"

echo "[3/7] rsync 代码（排除无关目录）"
rsync -az --delete --exclude .git --exclude node_modules --exclude .venv \
  --exclude __pycache__ --exclude '*.pyc' --exclude artifacts --exclude .worktrees \
  /Users/solarise/ZCodeProject/histopilot-suite/PathTogether/ "$REMOTE:$CODE_DIR/"

echo "[4/7] 构建镜像"
ssh "$REMOTE" "cd $CODE_DIR && podman build -t $IMAGE ."

echo "[5/7] 原子替换容器（单容器重启，不存在混合 worker 窗口期服务新 UI）"
ssh "$REMOTE" "podman rm -f $CONTAINER && podman run -d --name $CONTAINER \
  --network host \
  -v ~/svs-viewer-demo-data/uploads:/data/uploads \
  -v ~/svs-viewer-demo-data/share:/data/share \
  -v ~/svs-viewer-demo-data/sidecar-sessions:/data/sidecar-sessions \
  --restart unless-stopped \
  -e PORT=18080 \
  --env-file $AUTH_ENV \
  $IMAGE"

echo "[6/7] 冒烟（经 SSH 隧道逐条执行，详见 RUNBOOK.md §上线验收）"
ssh "$REMOTE" "curl -fsS http://127.0.0.1:18080/healthz >/dev/null && echo healthz=OK"
ssh "$REMOTE" "curl -fsS 'http://127.0.0.1:18080/api/demo/slides' -o /dev/null -w 'demo=%{http_code}\n' || true"

echo "[7/7] 提示：完整 G9 验收（真实 HTTPS/缓存迁移/荧光 preserve/30min 观察）按 RUNBOOK.md 执行"
echo "回滚：执行同目录 rollback.sh $ROLLBACK_TAG"
