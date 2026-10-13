# COS 经 fast 纯流式转发下载设计

日期：2026-10-13。状态：Review 与待实现设计。读者：后续实现及部署负责人。

## 结论与范围

目标是让腾讯云 fast 主机通过同地域内网下载 COS，再传给 PathTogether，降低 COS 公网下行费用。建议第一阶段保留浏览器直传 COS、现有上传任务和本地入库流程，仅替换大文件 GET 的传输路径。在现有定制 goapi 服务中增加切片专用流式下载通道。fast 边读取 COS 边向 PathTogether 发送数据，不保存切片、不注册 Gokapi 临时分享文件。

用户最初提到“上传下载转接”，后续明确主要目的为 fast 下载 COS 节约流量。因此本稿将上传改造列为非必需后续项，不把“所有上传已改走 goapi”作为第一阶段交付承诺。

用户已明确采用**纯流式转发、不落盘**。取消独立 100G 缓存、缓存索引、淘汰器和扩盘要求；不占 goapi 原有 200 GiB 文件配额。fast 只使用有界内存缓冲。**homepc 的暂存、断点文件和已入库持久切片仍按现有流程保存**，“不落盘”仅指 fast 转发链路。

本轮没有改运行代码、配置、容量、云盘或服务，没有上传测试对象或执行付费吞吐实验。

## 实际核对结果

| 项目 | 核对结果 |
| --- | --- |
| PathTogether review 基线 | release/viewer-skills-20261011，b723ab31 |
| 当前业务桶 | histopilot-1255456712，ap-shanghai |
| 当前生产 COS 中转池 | 10,000,000,000 字节；产品单次上传上限 9,500,000,000 字节 |
| fast 上 goapi | https://goapi.solarise94.fun；部署目录 /home/ubuntu/gokapi-relay |
| 定制镜像标签 | gokapi-relay:2.2.4-cos7d3 |
| goapi 自有桶 | goapi-1255456712，ap-shanghai，gokapi-relay/ 前缀 |
| goapi 200 GiB 口径 | 本实例元数据中远端文件大小，加 storage 目录本地文件；另保留 512 MiB |
| goapi 本地暂存上限 | 16 GiB；单文件上限 8 GiB |
| goapi 临时文件策略 | 最多保留 7 天，容量不足 FIFO 删除已完成文件 |
| fast 磁盘 | 只有 60 GiB 系统盘；检查时文件系统可用 28,208,406,528 字节，约 26.27 GiB |
| 实际内网解析 | 在 fast 上，两桶的区域域名均解析到 169.254.0.47 |

镜像 ID：`sha256:e6e23ff4098c74cfb8dbd74c4f94c2549654cf83eb6f22b2a686093b33074b34`。
服务附带源码包 SHA256：`e386dd6ee17d586bf30098212296f644b96cd81f7342faa662476dd708bd74d8`。本次审阅的是该部署源码包；未重新构建证明源码与镜像逐字一致。

只读源码副本在 `/mnt/ssd2/tests/goapi-design-20261013/Gokapi-cos`。未读取服务密钥内容。以下 Gokapi 路径以此源码树为根。

## Review 发现

### R1 P1 现有下载接口不满足版本化 Range 合同

PathTogether `cos_client.py:240` 使用 VersionId 和 Range；`cos_ingest_worker.py:530` 严格要求 206、正确的 Content-Range 起止和总长，默认按 8,000,000 字节读取一块。收到 200 会丢弃数据并计入重传预算，不能直接接受。

Gokapi `internal/storage/filesystem/s3filesystem/aws/Aws.go:230` 的 proxyDownload 不接收原始 request，GetObject 只有桶与 key，没有 Range 或 VersionId。它会取整个对象并复制响应体。因此简单把现有 GET URL 换成 UrlHotlink，会导致整文件重复传输、预算耗尽，且失去版本约束。它也只认识自己数据库中的文件，不能自然读取业务桶现有 incoming 对象。

**处置：新增专用通道；不要修改 worker 的校验来适应错误响应，不新增一次“业务桶复制到 goapi 桶”。**

### R2 P1 专用通道必须绕开 Gokapi 文件生命周期

`internal/storage/RelayQuota.go:90` 递归计数 QUOTA_ROOT，再在第 103 行累加本实例全部远端文件元数据；后续 FIFO 删除遍历文件库。

**处置：专用通道不调用上传、文件注册、容量预约或 DeleteFile/CleanUp，不创建暂存文件，不进入 files 表。** 因此与 goapi 的 200 GiB 配额、16 GiB 上传暂存上限、7 天过期和 FIFO 删除完全分离。

### R3 P1 不落盘需要覆盖应用与反向代理

仅在 Go 中使用流式复制不够：Nginx 若开启响应缓冲或缓存，慢客户端下载时仍可能把响应写入临时文件。

**处置：专用 location 显式关闭 proxy_buffering 和 proxy_cache，设置 proxy_max_temp_file_size 0，并关闭 gzip；应用返回 Cache-Control: no-store。** 不使用 ioutil.ReadAll、整对象 bytes.Buffer、下载到文件再发送等路径；SDK、中间件与故障重试路径也必须符合有界内存和不落盘约束。原主机磁盘容量不再是本方案的扩盘门槛。

### R4 P1 临时中转不能承担持久切片的唯一副本

goapi 的 7 天过期和 FIFO 删除会让链接失效；PathTogether 当前则在下载、验证和发布到 `UPLOAD_DIR/objects/<slide_id>` 后清理 COS 源。两边均不能作为“本地切片淘汰后随时回源”的承诺。

**处置：维持当前 homepc 本地持久资产。fast 不承担任何存储职责。** 原始对象已被清理时，新的回源请求应返回明确的 source_missing，不能伪装成空文件或改读另一个版本。

### R5 P2 迁移整个上传栈并不能直接实现最简设计

现有 goapi 普通 API key 具有账户级操作权限；上传请求专用凭证虽可限制文件数量和大小，但 `chunkreservation/ChunkReservation.go` 的预约存在内存 map 中。重启恢复、幂等完成、覆盖完整性等仍需逐项补齐验证；当前 8 GiB 上限也低于生产允许的 9.5 GB。

此外 `FileUpload.go` 使用进程全局 relayQuotaMutex，上传/完成持锁；不能让新增切片下载也争抢这个锁。

**处置：第一阶段不碰浏览器分块上传，不向浏览器暴露 goapi 服务 API key；专用下载仅使用独立并发限制，不调用上传/分享生命周期。** 若后续确需上传经 fast，另评估进度、续传、取消、幂等完成、任务隔离与上限，不因“有现成 API”跳过这些保障。

## 目标链路

```mermaid
flowchart LR
  B[浏览器或本机转换产物] -->|现有分块直传| COS[上海业务 COS 桶]
  PT[PathTogether 上传控制] -->|签名和完成 上传任务| COS
  W[homepc 摄取 worker] -->|认证 GET Range 固定版本| G[fast goapi 切片下载通道]
  G -->|同地域内网 Range GET| COS
  COS -->|固定版本 数据流| G
  G -->|有界内存 206 数据流 不落盘| W
  W --> V[大小 哈希 格式校验]
  V --> P[本地持久切片 发布与就绪探针]
  P --> U[Viewer 分享 AI]
```

浏览器看图仍由现有本地切片读取和瓦片服务完成；没有把每次拖动、AI 读图都改为远程拉 COS。上传状态文案和 API、ZIP/格式校验、配额结算、slide_id、标注分享权限、失败清理均保留。

fast 通道默认只供 PathTogether worker 调用，不对用户提供永久下载链接，不把原图 URL 暴露给浏览器。现有 Gokapi 200 GiB 分享业务继续运行。

## 最小实施面

1. goapi 定制构建新增独立 `slide relay` 模块：认证、固定 COS 源、版本化 Range 读取、有界流式复制、受限指标。复用已有 HTTP 服务与 Go SDK，不复用 files 表和临时分享生命周期。
2. PathTogether 增加一个薄下载适配器，保持 `get_object(key, version_id, range_header, timeout)` 的响应接口。将它用于摄取 worker 的 GET；其余 COS 控制操作继续使用现有 SDK。
3. 新增配置与私有凭证挂载，更新 CSP 无必要：浏览器上传目标没改。不要把服务密钥发到前端。
4. 不改 PathTogether 业务表结构，不新增上传状态机，不导入历史切片，不迁移 COS 对象，不修改 goapi 现有 200 GiB 上限。
5. `COS_DOWNLOAD_TRANSPORT=direct|fast` 为明确运维开关。上线选 fast；fast 故障重试/排队，**不自动回退公网 COS**。人工回退 direct 时接受相应费用并记录。

这仍需少量 Go 代码扩展，不能仅靠配置把现有通用下载接口接上。不要同时引入第二套文件管理平台或把所有 COS 控制职责迁走。

## 专用下载接口合同

以下为**拟新增接口**，不是现有可用 API。

`GET /internal/histopilot/v1/object`

- HTTPS；服务专用 token 从私有文件加载，PathTogether 与 fast 各存一份，不能复用管理员 API key。支持轮换期间两把 key；不记录 token。
- 以受控请求头传入对象 key、非空 VersionId、已确认总大小；`Range: bytes=start-end` 为必需。桶、地域、允许前缀由 fast 配置固定，客户端不能传 URL、endpoint、bucket 或任意请求头。
- 限定业务桶 `incoming/` 对象；拒绝越界、无版本、多范围、负数、过大范围。首版限定与现有 worker 一致的 8,000,000 字节对齐块及尾块，最大单次响应 8,000,000 字节。
- fast COS 身份仅允许业务桶指定前缀的读取/读取版本及必要元数据权限，无写入、删除权限；既有 goapi 桶凭证不能假定已拥有这项权限。
- 每次请求先认证和准入，再访问 COS。fast 构造区域 COS 请求，不跟随到任意外部源。SDK 请求固定 VersionId；检查上游 206、范围、对象总长和返回版本，版本或响应头错误时在发送下游响应头之前拒绝转发。
- 成功严格返回 206、Content-Range、Content-Length、实际版本；禁压缩，禁止 302 跳回 COS，代理层不缓冲完整文件。返回 `Cache-Control: no-store`，不设计缓存命中状态。
- 缺版本或非法范围：400/416；源不存在：404；调用者凭证错误：401/403；COS 凭证或权限错误映射 502 并告警，不能误报用户无权；服务并发满：503 与 Retry-After；源错误：502/504。错误响应小而固定，不含签名 URL/密钥。
- PathTogether 将错误映射到现有重试/终态路径；保留 206 与字节核验、最终 SHA256、readiness probe。fast 传输结束不等于切片入库成功。
- 连接、读空闲和总时限必须显式配置，request context 取消能终止 COS GET 和下游写入；实际值用 8 MB 块和 fast 真实出口带宽验收，不以无限超时兜底。

## 纯流式转发与资源边界

### 数据处理

1. 认证、验证 key/VersionId/Range，并尝试获取并发槽位；满时立即返回 503 与 Retry-After，不建立无界等待队列，也不先向 COS 发请求。
2. 用下游 request context 发起 COS GetObject，固定 VersionId 和 Range。收到上游响应后，先核对状态码、Content-Range、Content-Length、总大小及版本；失败则关闭上游 body，返回小型错误响应。
3. 校验响应头后立即发送下游 206，使用固定小缓冲读取并写出，不等整张切片或完整 8 MB 块到齐。限定读取总量为请求范围长度，禁止边转发边拉取整个对象。
4. 下游写入速度自然限制上游读取：写入阻塞时停止继续读 COS，不以后台 goroutine 和无界 channel 堆数据。应用、SDK、TLS 和 HTTP 层均不得按文件大小分配内存。
5. 正常结束、错误、取消、超时均关闭 COS body、释放并发槽位和缓冲。下游断开立即取消上游 context；无剩余订阅者后仍继续下载的后台任务不属于本设计。
6. 多个请求即使读相同范围也分别回源。首版不做内存对象缓存、single-flight 广播或后台预取，避免慢请求之间耦合和额外状态。

### 中途失败与续传

响应头发送前，可以返回 502/504；**206 已发送后不能再改状态码，也不能往切片字节流尾部追加 JSON 错误**。上游早 EOF、读错误、下游写错误或超时时，终止响应流；HTTP/1.1 以短于声明长度的断流体现失败，HTTP/2 终止相应流。记录结果类型和实际字节数。

PathTogether 继续以完整且正确的范围作为 checkpoint 单位。失败范围不前移 checkpoint，实际收到字节计入现有 wire 预算，下次重新请求该范围的同一 VersionId。fast 不存断点、不自动重放已发送的字节，也不在下游响应中拼接两次回源结果。请求发出前的 SDK 有界重试可保留，但必须受同一请求期限约束，计入 origin 指标。

### 初始资源参数

- `SLIDE_RELAY_MAX_INFLIGHT=4`：fast 单进程的专用通道并发上限。多实例部署时需重新核算总并发，不能把每进程上限当成整机上限。
- `SLIDE_RELAY_COPY_BUFFER_BYTES=65536`：每个活跃请求建议 64 KiB 复制缓冲；4 路约 256 KiB **只是应用复制缓冲**，不是整个进程 RSS 上限。若 io.CopyBuffer 走 ReaderFrom/WriterTo 优化路径，须确认实际缓冲仍有界，不能仅凭传入 buffer 大小推断占用。
- 首版允许范围仍为最多 8,000,000 字节，以保持现有 worker 合同；该数是传输范围上限，不是要求在内存中缓存一整块。
- 显式设置连接、读空闲、写空闲和总请求时限；Nginx 代理时限与应用期限协调，慢客户端最终可被释放。最终参数按真实出口带宽测试后确定。
- 保留原容器资源限制；在原 goapi 同时上传时测量总 RSS、文件描述符和延迟。是否需要调整容器限制以证据决定，不为该通道新增磁盘容量。

### 不落盘验收边界

Go 专用通道不创建切片、临时块或对象索引；Nginx 专用 location 不生成响应临时文件，不配置 proxy_cache，不改原 goapi 分享路由的行为。检查容器可写层、既有 storage、系统临时目录与 Nginx temp/cache 目录，不能仅检查一个自建目录。

现有服务可能正常写访问日志；日志只允许有限元数据，不记录响应体、完整 key、签名 URL 或认证头。验收“不落盘”指切片数据不作为文件写入应用或代理磁盘，不等同于宣称系统完全无磁盘写入或进程内存永不被操作系统换出。

## 费用与容量解释

**用户已确认本部署的两段传输均不产生流量费：**

| 链路 | 本方案计费前提 |
| --- | --- |
| COS → 腾讯云 fast | 同地域内网读取，无流量费 |
| 腾讯云 fast → HistoPilot | 按用户确认的现有主机计费条件，无流量费 |

这项结论针对当前部署，不泛化为所有腾讯云主机的公网出口均免费。腾讯云同地域 COS 内网传输规则见[官方流量说明](https://cloud.tencent.com/document/product/436/53863)与[内网域名说明](https://cloud.tencent.com/document/product/436/56556)。fast 出口的免费条件采用用户提供的事实，不再列为待确认问题。

因此，在上述条件下，迁移可消除原 COS → HistoPilot 直接公网下载对应的流量费用，不新增这两段链路的流量费，也没有缓存云盘费用。COS 原有存储、请求等非流量费用仍分别计算；失败重试可能增加请求次数，不能据此承诺整个存储服务零成本。

本次已验证业务桶与 fast 同地域、主机上区域域名内网解析。上线仍需确认容器内实际 DNS/连接路径，且 fast 不把下载重定向回 COS 公网、不自动直连兜底。这是验证实现遵守免费链路，不是重新质疑已确认的计费条件。

节费来源是路径改变，**不依赖缓存命中**。每次请求和重试均经 fast 读取 COS；分别记录 origin_bytes、egress_bytes、请求数及重试量，用于检查实际链路和异常开销。未进行大文件试跑，不给出未经测量的具体节省金额。

现有 COS 中转池 10 GB、goapi 文件库 200 GiB 和产品单文件上限保持不变。新通道没有磁盘缓存容量，也不需要给它配置 100G 配额。

## 实施顺序与验收

1. 固定当前 goapi 源码与镜像基线；明确新增模块的仓库位置。准备只读 COS 身份和服务认证，核对专用代理 location 不落盘，不改原 goapi 配额。
2. 实现专用流式通道，在隔离实例/测试桶验证；所有本机测试产物放 `/mnt/ssd2/tests/<独立运行目录>`。
3. PathTogether GET 适配器接入，保留旧接口合同和现有任务状态。增加 transport 配置，不替换浏览器上传 SDK。
4. 小范围启用 fast，观察 Range、错误、带宽、实际传输字节和内存占用；确认真实入库和读片后逐步放量。
5. 明确回滚到 direct 会重新产生 COS 公网流量；不需要迁移业务数据或清理已经成功入库的文件。回滚 goapi 时只停止新增通道，原分享服务保持可用。

| 验收案例 | 必须结果 |
| --- | --- |
| 业务桶大于 8 GiB 文件 | 专用 GET 可按块完成，不被通用 goapi 上传 8 GiB 限制误拦 |
| Range 中间块、尾块 | 206、长度、范围、总长均正确；不会返回整文件 200 |
| key 相同 VersionId 不同 | 严格读取请求版本，旧版本不会冒充新版本 |
| 错误上游响应头 | 未发送下游 206 前拒绝，返回固定错误，不转发错误对象 |
| 早 EOF/上游断连 | 已发 206 后中止响应，不追加 JSON；worker 不提交该范围 checkpoint |
| 下游断连/取消/写超时 | COS 请求及时取消，body 关闭，槽位归还，不继续后台拉流 |
| 同块与不同块并发 | 各自流式回源，不卡住原 goapi 上传；超限立即 503，无无界排队 |
| 慢客户端与大文件 | 无整块/整文件堆积，RSS 随受限并发而非文件大小增长 |
| 断点重试、worker 换代、取消 | 沿用现有 fencing 和清理；无晚到写入或额外发布 |
| goapi 200 GiB 满或正在 FIFO 清理 | 专用转发不受文件库容量闸门影响，也不触发原文件淘汰 |
| 应用与代理不落盘 | 正常、慢速、超时、失败路径均无切片临时文件或代理缓存文件 |
| fast 在传输中重启 | worker 保持上次完整范围 checkpoint；重新请求同版本，无恢复缓存步骤 |
| fast 不可用 | 排队重试；抓包确认无自动 COS 公网兜底 |
| token 无效、任意 URL、越界 key | 先拒绝且零 COS 读取，不泄露凭证与对象信息 |
| 摄取完成并清理 COS 源 | 切片列表、info、thumbnail、tile、分享页、AI 读图仍正常 |
| 停止 fast 服务后读取已入库切片 | 本地 Viewer、分享及 AI 读图正常，不依赖 fast 持有副本 |
| 内网与费用 | 容器内确认内网回源；分别记录 COS origin bytes、fast egress 和重试量，核对账单 |

指标只按低基数结果类型聚合：origin_bytes、egress_bytes、range_errors、inflight、busy_rejections、cancellations、timeouts、retries，以及进程 RSS/文件描述符。不要把 key、文件名或用户 ID 作为监控标签。

## 交接约束

- 已确认：经腾讯云 fast 下载 COS；fast **只转发、不落盘、不缓存**。两段链路均无流量费，按用户确认的当前部署计费条件设计。取消先前 100G 缓存和新增云盘设计。
- 第一阶段保留浏览器 COS 直传与 homepc 本地入库，仅替换大文件下载传输。
- 初始并发 4、64 KiB 复制缓冲为设计建议，需结合原 goapi 同机负载实测；不得把 256 KiB 复制缓冲当成进程总内存承诺。
- 上线前必须验证：容器实际内网连通、业务桶只读授权、服务认证、Range/版本合同、不落盘、背压、取消传播和中途失败语义。
- 未验证：真实大文件吞吐、转发与原上传业务争用情况、源码包与镜像可复现构建。此文是设计评审，不是实现完成或上线验收报告。
