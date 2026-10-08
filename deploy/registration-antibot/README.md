# 注册防刷发布

生产基线：`suite-20261008-domains`，image ID `6aaea3a020648f5acb5d82f8f424bae8bb5f7e040023b8bed90a0d640ff2ce6e`。
候选：`suite-20261008-registration`；镜像 ID 和代码 revision 固定在发布目录的 `release.json`。

发布目录：homepc `/home/solarise/releases/suite-20261008-registration`，权限 0700。
公开配置在 `deploy.py` 的 `EXTRA_ENV` 中；secret 仅从该目录的 `turnstile.secret.env` 读取，权限必须为 0600。目录内 env、数据库快照和容器配置均属于私有发布记录，不进 Git，不打印内容。

当前 widget 的控制台配置已由用户确认：托管模式、允许 `.cn` 与 `.com`。控制台额外允许的 localhost/旧域名不加入服务器生产 hostname 白名单。

## 安全存入密钥

从 Cloudflare widget 的密钥区域复制 **secret key**，然后在自己的交互终端运行：

```bash
ssh -t homepc 'python3 /home/solarise/releases/suite-20261008-registration/deploy.py store-secret'
```

输入隐藏，不经过聊天、命令行参数或 shell history。该命令只保存文件，不部署、不发送邮件。公开 sitekey 不能替代 secret；当前未发现已保存的生产 secret 或 Cloudflare API 凭据。

## 发布步骤

在 homepc 的发布目录中运行：

```bash
python3 deploy.py preflight
python3 deploy.py accept-check
python3 deploy.py prepare
python3 deploy.py quiesce-check
```

`accept-check` 使用生产库只读快照，在独立 PostgreSQL 容器中恢复并应用 0079；所有验收 worker 关闭，生产可写挂载换成发布目录下的隔离目录。结束时删除验收容器，并确认生产迁移记录未变化。缺少 secret 时仍可验证缺配置的页面与镜像，但这种验收记录不能满足 `prepare` 的上线条件；保存 secret 后须重跑验收。

确认生产 secret、隔离验收、候选镜像和停止中的待切换容器均已准备，再由用户批准切换：

```bash
python3 deploy.py cutover-pt --approved
python3 deploy.py post-check
```

`cutover-pt` 在切换前后核对未完成上传/转换等任务；停止旧进程后再做一次数据库备份，新镜像启动才对生产库应用 0079。启动/健康检查失败会恢复旧容器。生产配置必须与隔离验收时完全一致。

回滚：

```bash
python3 deploy.py rollback
```

0079 只增加列和表，容器回滚保留该迁移与上线后数据，不自动执行数据库还原。旧程序忽略新增字段；新规则产生的 redelivery 在旧程序中不会排水，回滚后需保留记录，待恢复新版本后再处理。

## 真实密钥实测

- `.cn` 和 `.com` 分别打开注册视图，核对挑战加载、CSP、help 页和语言。
- 仅使用由用户指定/控制的测试收件邮箱触发实际邮件；核对相应域名、站点名、语言和帮助链接。
- 测试挑战 token 重放拒绝时使用新的 submission ID；同 submission ID 的网络重试应幂等回放，不能误把它当成防刷失败。
- 国内网络、微信/邮箱内置浏览器需要真实设备验收，桌面 Chromium 的模拟不能代替。
- 邮件服务器接受不代表进入收件箱，继续检查实际投递或退信。

SMTP 密码此前出现在工具输出的事件单独处理；本次发布不擅自替换该凭据。日志和部署核对仅输出键名及是否配置。
