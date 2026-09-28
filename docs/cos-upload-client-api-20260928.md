# 统一上传客户端接口说明（COS-only，U5 检查点 B 后）

日期：2026-09-28。适用：PathTogether `ec06f84` 及之后构建。旧接口
`POST /api/upload`、`POST /api/uploads`、`PUT /api/uploads/<id>/chunk`、
`POST /api/uploads/<id>/commit`、`GET /api/uploads/<id>`、
`DELETE /api/uploads/<id>` **已删除**（检查点 B，随 410 过渡窗口一并退役）。
所有新上传统一走 `/api/ingestions`（浏览器字节直传 COS，平台只处理控制
面 JSON）。本文是唯一客户端合同；调用方迁移遇到旧端点应视为自身缺陷。

## 1. capability 协商（唯一参数来源）

`HP_APP_BOOTSTRAP.capabilities.cos_upload`（`_app_capabilities` 随页面下发）：

```json
{
  "available": true, "manual_only": true, "policy_version": "v1-manual",
  "formats": ["bif", "bmp", "jpeg", "jpg", "kfb", "kfbf", "ndpi", "scn",
              "svs", "svslide", "tif", "tiff", "vms", "vmu", "zip"],
  "max_size_bytes": 10737418240,
  "part_bytes": 32000000, "url_ttl_seconds": 900,
  "max_concurrent_parts": 3, "sign_batch_max_parts": 8
}
```

- `formats` 从格式注册表派生（后端权威）：原生单文件 + zip + convert-required
  （kfb/kfbf）；**裸 .mrxs 不在列**（须连同数据目录打包 zip）。
- `max_size_bytes` = 产品上限（单一服务端权威；不随池瞬时余额变化）。
- `available:false`（capability off / 池配置门禁未过）→ 客户端**禁用创建**
  并提示上传暂不可用；不得回退其它传输。

## 2. 创建任务

`POST /api/ingestions`（会话 + CSRF）：

```json
{"filename": "slide.zip", "declared_size": 12345,
 "sha256_expected": "<64 hex，可选>", "idempotency_key": "<可选 ≤128>"}
```

响应 202 + 任务状态体（下同）。错误（均不建行、不占预约）：

| 码 | HTTP | 含义 |
|---|---|---|
| `invalid_declared_size` | 422 | size 非正整数 |
| `cos_format_unsupported` | 422 | 格式不在受理集（裸 mrxs 提示打包 zip） |
| `upload_too_large` | 413 | 超产品上限（body 带 max_size_bytes） |
| `cos_pool_below_product_limit` | 503 | 服务端容量配置门禁未过（联系运维） |
| `cos_waiting_limit` | 409 | 该身份已有等待中任务 |

池余额暂时不足 → 202 + `state=waiting_capacity`、`code=cos_waiting_capacity`、
`queue_position`；`expires_at` 为等待绝对期限。按 5s 轮询状态。

## 3. 状态体（GET /api/ingestions/<job_id>；本人或 owner，其余 403）

```json
{"job_id": "inj_…", "kind": "zip|native|conversion", "state": "…",
 "stage": "waiting_space|uploading|awaiting_server|downloading|validating|processing|readiness|viewable|terminal",
 "declared_size": 12345, "format_ext": "zip", "fail_code": null,
 "viewer_ready": false, "cleanup_status": "none", "local_cleanup_status": "none",
 "expires_at": 0, "slide_id": "sld_…", "slide": "display-name",
 "items": [{"item_key": "a.tif", "slide_id": "sld_…", "state": "published", "fail_code": null}],
 "conversion": {"job_id": "cvj_…", "state": "queued|converting|ready|failed", "canonical_name": "a.tif", "slide_id": "sld_…"},
 "downloaded_bytes": 0}
```

- `kind=native`：`slide_id` 创建即绑定，viewable 后可打开。
- `kind=zip`：`items` 逐逻辑切片结果（item 级失败不株连其它）；打开目标
  取首个 `state=published` 的 item。
- `kind=conversion`：`conversion` 子视图跟踪转换（`slide_id` 仅 ready 出现；
  转换失败经 `POST /api/conversions/<job_id>/retry` 重试，上传任务保持
  ready 不回滚）。
- `stage=processing` = 服务端解包/转换中；`uploading` 阶段带 `parts`（冻结
  分块计划，编号+长度）。

## 4. 分块上传（唯一字节通道：浏览器 → COS）

1. `stage=uploading` 后按计划分批 `POST /api/ingestions/<id>/parts/sign`
   （`{"part_numbers":[…]}`，单批 ≤ sign_batch_max_parts，每分钟限频）→
   绑定 Content-Length 的 UploadPart 预签名 URL（短 TTL 可续签同 uploadId）。
2. 对 URL 直接 `PUT`（**独立传输**：不带平台 Cookie/CSRF；mode=cors、
   credentials=omit）。
3. 全部块确认后 `POST /api/ingestions/<id>/upload-complete`（幂等；409
   state conflict 视为已完成过）→ 服务端核验下载。
4. 恢复：`POST /api/ingestions/<id>/resume`（completing 回 uploading 续传）；
   单块失败重签重传（同编号覆盖）。同名重选文件须确认同一任务身份——
   worker 以可信 ListParts/版本核对为权威，浏览器确认仅提示。

签名错误：`local_reservation_invalid`(409)、`cos_capacity_reconcile_required`
(503)、`cos_sign_rate_limited`(429)、`plan_not_ready`(409)。

## 5. 取消与终态

`POST /api/ingestions/<id>/cancel`（幂等）。commit intent 持久化后拒绝
（409 `commit_in_progress`——落库后删除走切片删除合同）。终态：
cancelled / failed / expired / completed（`fail_code` 携带稳定机码，如
`hash_mismatch`、`zip_rejected`、`zip_quota_exceeded`、`invalid_kfb_header`、
`reservation_expired`）。

## 6. 迁移说明（旧客户端）

- 收到 404（旧端点）= 调用方待迁移；不存在兼容新建路径。
- 旧 V2 续传语义 → 新链路的分块确认/签名重取（§4）；旧恢复记录
  （localStorage）应清除。
- 服务器时间戳不作为任何资格证明。
