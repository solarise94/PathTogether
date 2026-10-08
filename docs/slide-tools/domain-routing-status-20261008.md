# 域名分流状态 — 2026-10-08

## 已上线

- 应用 `9eef18c3e210e106d3dcdd31b28697fde8db1327`，镜像 `suite-20261008-domains`，ID `6aaea3a020648f5acb5d82f8f424bae8bb5f7e040023b8bed90a0d640ff2ce6e`。
- 与上一镜像比较，338 个应用文件中只改变 `static/i18n.js`。无迁移或插件变更，后台插件继续 0.4.15。
- 无已保存语言偏好时，histopilot.cn 默认中文，histopilot.com 默认英文；明确保存的用户偏好优先。
- homePC pt-edge 的两个旧域名 TLS listener 已切为 308 到 `https://histopilot.cn$request_uri`；保留 ACME challenge 供旧域名证书续期，其余路径均跳转。
- 用户明确选择全站跳转，接受旧域名 OPFS/续传数据暂时无法访问。未删除任何浏览器数据。

## 实测

- 5 组语言判定检查通过。
- 公网真实 Chromium：中文浏览器访问 .com 默认英文，英文浏览器访问 .cn 默认中文；两站手动切换语言后刷新仍保留。
- 公网旧域名 `/tools/slides?test=domain%20route` 返回 308，Location 路径及编码参数保持不变。
- 候选镜像、构建清单、92 个静态文件及 CSP 验收通过。
- 切换前后进行中任务均为 0，备份保留，生产健康且 sidecar reachable。

## 尚未上线：中国 IP 的 .com → .cn 跳转

`deploy/domain-routing/international-nginx.conf` 是待部署配置，不代表公网已生效。
原 SSH 别名 kuaikuaiyun（23.251.34.206）的 histopilot.com vhost 已不是实际公网回源：来自中国/美国 Cloudflare 的探测均未到达该入口。已撤回该旧入口的临时配置和诊断日志变更，恢复其原配置。

homePC 另有到 DMIT 179.255.99.118 的回源隧道；该线索尚不能独立证明 Cloudflare 当前唯一源站。已向用户询问当前国际站 SSH 连接方式；现有凭据和 known_hosts 尚不能建立受验证连接。未绕过主机密钥校验。

后续需在实际入口部署并验证：可信 Cloudflare 来源的 CN 页面请求 302 到 .cn；US/未知国家不跳；直连源站伪造 CF-IPCountry 不生效；API、静态、健康请求不跨域跳转。中国与美国实际出口已可用来验收。

## 回滚和证据

- homePC 发布目录 `~/releases/suite-20261008-domains/`；私有本地证据 `.gate-tmp/domain-routing-20261008/`。
- 应用回滚：`python3 ~/releases/suite-20261008-domains/deploy.py rollback`，恢复 `suite-20261008`（保留插件 0.4.15）。
- 旧域名恢复：将该发布目录 `pt-edge-before.conf` 复制回 `~/.config/pt-edge/nginx.conf`，运行 nginx -t 后 `systemctl --user reload pt-edge`。
- 本次 Git 未推送。
