# image-transport-upgrade 发布与回滚 Runbook

适用目标：homePC Demo（唯一已授权部署形态，依据 docs/demo-deployment.md 2026-08-31 更正版）。
HP 无业务改动 → **不重部署 HistoPilot**，仅做对拍与在线终态验证。

## 发布前（一次性）
1. 确认发布 SHA（deploy.sh 内 SHA_EXPECTED 已回填为 B8 提交 SHA）。
2. `ssh homePC "podman images | grep svs-viewer-demo"` 记录当前镜像摘要。
3. 确认权威 env-file `~/.config/pt-deploy/pathtogether.env`（含 REQUIRE_ADMIN_AUTH=1）未被改动；
   荧光 flag `PATHTOGETHER_MULTICHANNEL_ENABLED` 沿用原值（本方案不以其作回滚手段）。
4. 本次**无 DB schema 迁移**、不改 uploads/share/session 数据；备份仅镜像标签即可。
5. 混合 worker 检查：部署形态为单容器原子替换（podman rm -f + run），不存在新旧 worker
   同服窗口；如未来改多容器滚动，必须先确保全部 worker 支持新协议再放量新 UI。

## 部署（执行 release 目录 deploy.sh）
脚本步骤：校验 SHA → 旧镜像打 rollback 标签并记录 digest → rsync → podman build →
原子替换容器 → healthz 冒烟。所有命令已按上述拓扑填入，shell 语法检查通过（bash -n）。

## 上线验收（G9 清单，未执行——本交付不含部署授权）
- [ ] 确认容器内 PT SHA 与镜像摘要对应最终提交；JS 资源 `?v=20260906a` 生效（查看 index.html 引用）。
- [ ] 经 SSH 隧道（`ssh -N -L 18080:127.0.0.1:18080 homePC`）走可信链路，不走公网 41083 明文入口。
- [ ] 授权样本：RGB 两档（标准/精细 tile URL 带 `?profile=&dv=`，实际 JPEG 采样分别 4:2:0 / 4:4:4）；
      2/4 通道荧光 preserve（tile q95/4:4:4；thumbnail 同 4:4:4）；tile 与 thumbnail 的 context 一致。
- [ ] 主站/Demo/share 三入口正常浏览；匿名 401、撤权后同 ETag 不得 304、过期 share 404。
- [ ] 真实 HTTPS（Caddy/frps 终止 TLS 后）缓存 query、ETag、304、旧一年 immutable 缓存迁移
      （旧页面强刷一次即可取新 UI；已缓存旧 immutable 响应无法服务端撤销——发布说明注明）。
- [ ] DPR=2、精细切换、通道切换网络与截图证据。
- [ ] 既有多通道 AI 会话恢复 + 新 run 均 finished；无新增内容哈希漂移/重复概览物化/会话重绑。
- [ ] 受控样本冒烟后观察 ≥30 分钟：无新增 5xx/409 循环、RSS 稳定、编码尾延迟/带宽在预算内
      （内部指标：`GET /internal/viewer-metrics`，X-AI-Internal-Token 门槛）。

## 回滚（执行 release 目录 rollback.sh <rollback 标签>）
- 同一兼容组合恢复旧镜像+旧前端；不动 `PATHTOGETHER_MULTICHANNEL_ENABLED`、session、原片、权限。
- 本次新增 viewer 派生缓存为进程内存 LRU（TILE_CACHE_MAX/TILE_CACHE_MAX_BYTES），容器重启即清空；
  不清 AI/checkpoint 缓存，无全局目录删除。
- 回滚后必须验证页面实际只发旧协议请求（Network 面板确认 tile URL 无 `?profile=`）；
  旧服务端对新 URL 忽略 query 正常 200（本地演练 §6 已证），禁止无限刷新循环。
- 回滚冒烟三件套：RGB 显示 / 分享权限 / 旧 AI session 恢复。
- 再次上线：使用正确的新编码身份（dv 随 encoding fingerprint 变化自动隔离），不复用错误配置的 dv。

## 回滚触发条件（§10.4）
颜色/通道语义改变、荧光被编成 4:2:0、旧会话哈希异常、权限缓存错误、版本刷新循环 → 立即停止放量。
性能预算持续超限 → 先退新 UI 到旧显示路径，不得降荧光 q 或改通道配色应急。

## 发布记录（B8 交付时点）
- 发布头 SHA：`cf3cf58`（完整 `git rev-parse HEAD` 于执行时复核；PathTogether wip/ser8-dev，本地提交未 push）
- 主实现提交：`826f40b`；发布记录：`600c8e3`；交付摘要：`cf3cf58`
- 部署命令：`./deploy.sh $(git -C ~/histopilot-suite/PathTogether rev-parse HEAD)`
- HP：无生产代码变更，不重部署
