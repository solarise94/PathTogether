# COS fast 纯流式转发：实现与本机验收记录

日期：2026-10-13。对应设计：`docs/cos-fast-goapi-relay-design-20261013.md`（dc5f5b55）。
状态：**实现完成、本机验收通过；未部署、未推送、未接触真实 COS 或 fast 主机。**

## 代码位置

| 仓库 | 分支 / 提交 | 内容 |
| --- | --- | --- |
| PathTogether | `cos-fast-relay`，基于 dc5f5b55，至 16784bee | `cos_fast_relay.py` 薄适配器；`cos_client.get_object` 按 `COS_DOWNLOAD_TRANSPORT` 分发；worker 仅多传 `object_size` |
| gokapi-relay（新建本地仓库 `histopilot-suite/gokapi-relay`） | `main`，基线 4014b02 = 已部署源码包（sha256 e386dd6e…），至 7926715 | `internal/sliderelay/` 专用通道、`deploy/nginx-slide-relay.conf`、`deploy/slide-relay.env.example`、`docs/slide-relay-contract.md`（线合同）、`docs/slide-relay-ops.md`、`e2e/` 验收脚本 |

gokapi-relay 目前只有本地 git，无远端；部署前需决定其托管位置。

## 实现要点（对照设计）

- 接口：`GET /internal/histopilot/v1/object`，Bearer token（文件 1–2 行，支持轮换），`X-HP-Object-Key/Version-Id/Object-Size` + 单段 `Range`；8,000,000 字节对齐块与尾块。
- 认证、校验、并发槽均在访问 COS 之前；满槽立即 503 + Retry-After，无排队。
- 独立 aws-sdk-go v1 会话与只读凭证文件，不用 goapi 自有凭证；COS 传输 `Proxy=nil`，endpoint 只允许 https（回环地址除外）。
- 上游 Content-Range/Content-Length/版本在发送 206 前核对；206 后中途失败一律 `http.ErrAbortHandler` 断流，不追加 JSON。
- 固定 64 KiB 缓冲手写复制循环，不用 io.Copy；上游读空闲与下游写期限均受 `SLIDE_RELAY_IDLE_TIMEOUT` 约束；断连取消 COS 请求并归还槽位。
- 不引用 Gokapi files 表、RelayQuota、chunk 预约、storage 或清理路径（import 核对为零）。
- PT 适配器：`trust_env=False`、不跟随重定向、`Accept-Encoding: identity`；非 206 → `CosAdapterError`；206 但版本/编码/长度不符 → `CosAdapterError`；中途断流 → `read` 返回 `b""`，worker 按早 EOF 记 wire、不推进 checkpoint。配置缺失或未知 transport → `CosConfigMissing`，**从不自动回退 direct**。

## 验收结果

| 检查 | 结果 | 运行目录（SSD2） |
| --- | --- | --- |
| Go `go vet` + `go test -race` sliderelay（含 webserver 包 `-tags=test,awstest`） | 全部通过 | run-gorelay-accept-X0NERY（主代理独立复跑） |
| PT COS 相关 18 个测试文件 | 274 passed | run-ptfast-accept-c8037c |
| PT 全量（排除 test_e2e_pg_reap.py、tests/e2e） | 2966 passed / 127 failed / 113 skipped；127 个失败在 dc5f5b55 原样复现，分支独有失败 0 | run-ptfast-full-Z68UyB；基线 run-ptbase-k00JbU |
| 真实镜像 E2E（PT `_fetch_range` → Nginx → Gokapi 镜像 cdb6b38 → 假 COS） | 25/25 PASS | run-e2e-fastrelay-UNouZI |

127 个既有失败原因：本分支 `requirements.txt` 要求 `tifffile==2026.9.20`、`zarr>=3.4.1`，而共享 `.venv` 仍为 zarr 2.18.7 / tifffile 2024.5.22（`ZarrTiffStore(read_only=…)` 等），与本次改动无关。

E2E 覆盖：多块完整对象两个版本 SHA 一致且互不冒充；9 GB（>8 GiB）对象中间块与尾块；上游早 EOF → worker `(None, 4000000)`；版本/范围/总长不符 → 502 upstream_mismatch；COS 403 → 502 upstream_auth；缺 key/缺版本 → 404 source_missing；越界前缀 403、未对齐 416、错 token 403、轮换 token 成功，且被拒请求对 COS 零读取；上游卡死 → 5 s 空闲中止、worker 记 1 MiB；并发 2 时第三个立即 503 且零 COS 读取；客户端中途关闭 → 假 COS 观察到断连、inflight 归零、cancellations+1；传输中重启 Gokapi → worker 得到短体不采纳，重启后同版本同范围一次重试成功。

不落盘：Nginx 使用故意恶劣的全局默认（gzip on、proxy_buffering on、允许临时文件、sndbuf 8k + 4 KiB 接收窗口慢客户端制造背压）。对照 location（强制忽略 X-Accel-Buffering）记录到 `an upstream response is buffered to a temporary file`，证明探针有效；转发 location 在 64 次请求中为 0。Gokapi 容器 `podman diff` 只有挂载点与 podman 注入的 `/etc` 条目，`/data` 只有 sqlite 与 log.txt。应用本身返回 `X-Accel-Buffering: no`，即使误用默认 location，Nginx 也不缓冲。

资源：两路 1 MB/s 慢客户端各 16 MB 期间，Gokapi RSS 30.6→30.8 MB、FD 11→14，不随传输量增长。

日志：Gokapi 每请求一行（结果、origin/egress 字节、耗时、key 形状 `incoming/u/<4 segments>`）；Nginx access log 只有方法与路径；token 与完整 key 在两侧日志中出现次数为 0。

## 审查轮次

- PT 第 1 轮：补 206 后版本头、Content-Encoding、Content-Length 核对；8 MB 块早 EOF wire 精确计数（16784bee）。
- Go 第 1 轮：COS 传输取消 `ProxyFromEnvironment`；endpoint https 限制；origin/egress 分开计数并修 short write 多计；`retries` 指标；头前断连归为取消；Nginx 片段去重 Cache-Control、加 `send_timeout`（369058c、50776c2、f65705f）。
- Go 第 2 轮（E2E 发现）：写期限触发的中止被记为 canceled，改为 timeout；运维文档 token 生成不回显、CAM 动作名标注待核、回滚措辞（cdb6b38）。

## 上线前仍须完成（需用户批准）

1. 确定 gokapi-relay 仓库托管位置；在 fast 上用该提交构建镜像并核对与旧镜像差异仅为新增通道。
2. 创建只读 CAM 子账号（`GetObject`/`HeadObject` 限 `incoming/*`；版本相关动作名须在控制台核对），密钥只落 fast 私有文件。
3. **真实 COS 核对**：带 VersionId 的 Range GET 是否返回 `x-cos-version-id`/`x-amz-version-id` 与 Content-Range；本机只用假 COS 验证。
4. fast 现有 Nginx 中加入转发 location（`proxy_pass` 上游名按现有配置调整），容器内确认 `cos.ap-shanghai.myqcloud.com` 解析为内网地址。
5. PathTogether 发布：新 env（`COS_DOWNLOAD_TRANSPORT`、`COS_FAST_RELAY_URL`、`COS_FAST_RELAY_TOKEN_FILE`、两项超时）须加入该 release deploy.py 的 EXTRA_ENV 白名单，token 文件 0600 只读挂载；先以 `direct` 发布，再切 `fast` 小范围观察。
6. 真实带宽下验证 8 MB 块耗时与 `SLIDE_RELAY_IDLE_TIMEOUT`（生产保持 ≥30 s）、指标与账单核对。
