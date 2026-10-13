# 站点统计、转换保存及 Logo 发布（2026-10-13）

已于 **2026-10-13 10:49:18（UTC+8）** 切换生产，随后线上 Dogfood 通过。

- PR：[#12](https://github.com/solarise94/PathTogether/pull/12)，已 squash 合入 `main@169454b5ab2e9d1ff077bba82c730206544b72df`。
- 实际运行源码版本：`c8697a495112fedbba0041d05cb3062ad30c51b2`。后续 `486752fe`、`4303217d` 仅修改测试；361 个镜像内文件与最终 PR head 逐项哈希一致，最终 PR 与合并后的 main 内容一致。
- 镜像：`localhost/pathtogether-demo:site-branding-stats-20261013`。
- 镜像 ID：`1c804cbf5c0b50599bd0b201c93d869f791b49b8bbd5f7100b8ecb76cddb8c6a`。
- 生产迁移：`0083_site_visit_dedup_dimensions.sql` 已应用。
- 原服务的环境、挂载、预算及重启设置保留，唯一环境差异为 `APP_REVISION`。

## 门禁与上线前演练

[最终发布 CI](https://github.com/solarise94/PathTogether/actions/runs/38336746971) 四个 job 全绿：

| 检查 | 结果 |
|---|---|
| 全量 Python | 3064 通过，115 跳过；覆盖率 82.32% |
| 全量 JS | 1115 通过，2 跳过 |
| 全量浏览器 | 121 通过 |
| PG canary | 通过 |

CI 期间发现并修复了两种测试适配遗漏：工具栏夹具不支持新增 Logo 模板 include；两个 Python 测试仍要求空 favicon。工具栏原行为断言保留；图标断言改为真实 ICO 内容、匿名可读、无会话 cookie。

生产数据库副本中验证了：0083 重跑成功，既有访问事件的全行校验和不变；转换页在两个域名、真人/爬虫两类请求下分别记录 4 条事件；兼容回滚统计写入成功。ID-only OME 切片读取、info 和 thumbnail 都返回 200。演练容器的写挂载和数据库均隔离，生产迁移状态在演练前后不变。

初次构建遇到复用历史镜像的 overlay 层数上限。改用 `--layers=false --squash-all` 后构建成功；应用依赖与原生产镜像一致，没有下载安装新的运行依赖。

## 线上 Dogfood

- `.cn`、`.com` 的健康检查正常，105 个静态文件逐项与镜像清单匹配。
- 首页 1440px / 390px 无横向溢出，Logo 加载正常；公开指南、地图及 LLM 文档均可访问。
- favicon 返回 200，匿名访问不创建 cookie；未登录工作台仍返回登录跳转。
- 转换页访问已在两个域名分别入库。本次自动验收的 UA 明确带 Headless 标记，记录为疑似爬虫，不冒充真人使用。
- 真实线上 WASM 转换合成 KFB/BMP/JPEG 成功，导出字节与页面 SHA 一致，tifffile 可解码为 300×580×3 RGB OME-TIFF；刷新后任务保留。
- 取消保存、保存失败提示保留；缺少保存 API 时，选文件前警告可见；PNG 拒绝说明已更新。
- 本地转换流程无 `/api/` 请求或 JavaScript 异常，没有上传这些合成切片到生产。
- 无头浏览器保存选择器用 OPFS 文件句柄替身；未宣称实际操作 OS 保存对话框或覆盖全部扫描仪/多 GB 切片。

现在记录的是**转换工具页面访问**；本地转换完成次数仍未上报，历史转换使用量也没有猜测回填。

## 备份、回滚与证据

远端目录：`~/releases/site-branding-stats-20261013/`，权限私有。停写前后都做了生产备份，最终 `precutover.dump` 为 3,615,665 bytes，667 个 TOC 条目。

原容器 `pathtogether-demo-pre-site-branding-stats-20261013` 保留。另准备了 `pathtogether-demo-rollback-ready-site-branding-stats-20261013`，使用旧应用加兼容 0083 的统计写入器，避免回滚后旧三维冲突键失配。兼容镜像 ID：`d1e508d71b1ab3140ddea45c6a1dcea61a40f91a44b8d16389edf5ae486bf458`。

回滚命令（本次未执行）：

```sh
ssh homepc 'cd ~/releases/site-branding-stats-20261013 && python3 deploy.py rollback'
```

回滚保留 0083 与新访问数据，不恢复旧约束或删除新事件。发布脚本会核对容器及镜像身份；当前生产配置发生变化时须重新评估。

本机证据在 `/mnt/ssd2/tests/deploy-PCaYpZ/`：CI 日志、`source-equivalence.json`、镜像清单、迁移演练、备份/切换记录、公开页面及转换 Dogfood JSON、导出验证和截图。敏感配置与数据库备份只保存在远端私有发布目录，没有提交到仓库。
