# 百度导入修复部署（2026-09-16）

- 目标：SSH `homepc`，容器 `pathtogether-demo`。
- 镜像：`localhost/pathtogether-demo:baidu-review-20260916`。
- 镜像 ID：`8f5e199848ac81a780fd6cb61dd1fc4103653e34ff6f05f834aef7840314b08d`。
- 基础镜像：线上 `localhost/pathtogether-demo:testapp-20260916`。
- 本次仅更新 `/app/baidu_import_store.py`；路径、进度文案、worker 脚本等线上已与本地一致。
- 文件 SHA-256：`1163d934d40c183ef53ba9a9cbdd3c2a6acf8d200501c31180a2f795b0dbd4ad`。
- 远端发布目录：`/home/solarise/releases/baidu-review-20260916`（0700；运行配置快照 0600）。
- 远端源码同步到 `/home/solarise/pathtogether-demo/baidu_import_store.py`；原文件保存为发布目录 `source-before.py`。
- 原容器保留为 `pathtogether-pre-baidu-review-20260916`，已停止。

验证：

- 发布前回归：98 passed。
- 新旧容器环境变量、挂载、启动命令、网络与资源配置一致。
- 切换前无执行中的百度枚举或导入批次。
- 源站 `/healthz` 返回 200，PostgreSQL 正常，sidecar reachable。
- 工作站 curl 验证 `https://histopilot.com/healthz` 和 `https://pt.solarise94.fun/healthz` 均为 200、ok=true。
- 容器内百度 worker 常驻进程数为 1。
- capabilities 的 enumeration_available / import_available / worker_enabled 均为 true。
- 当前连接器环境下 `bdpan whoami --json --no-check-update` 返回退出码 0（未记录账户输出）。
- 镜像内文件哈希与发布文件一致。未执行真实分享转存/下载验收。

回滚（在 homepc 执行）：

```sh
podman stop --time 45 pathtogether-demo
podman rename pathtogether-demo pathtogether-baidu-review-20260916-failed
podman rename pathtogether-pre-baidu-review-20260916 pathtogether-demo
podman start pathtogether-demo
curl --fail http://127.0.0.1:18080/healthz
cp /home/solarise/releases/baidu-review-20260916/source-before.py /home/solarise/pathtogether-demo/baidu_import_store.py
```
