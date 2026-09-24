# COS 控制台配置清单（Phase 0 PoC，操作者执行）

> 面向持有腾讯云控制台权限的操作者。合同依据：docs/cos-direct-upload-audit-plan.md
> §10"PoC 环境边界与执行记录"与 Phase 0 第 1 步；§5 CORS 要求；§6.2/§6.3 生命周期与碎片清理。
> **本清单在操作者执行前是待办**；每项执行后在文末"执行记录"打勾并填写实际值（脱敏：
> 不写 SecretKey/Token；bucket 名与 region 可写，它们不属于秘密）。

## 0. 前置决策：复用现有桶 vs 专用测试桶

先填下面的决策记录，再执行任何桶级变更。依据合同 §10："`poc/` 只隔离对象路径。
版本控制是桶级设置……不能安全隔离配置影响时采用专用私有测试桶，不直接改变生产桶。"

### 决策记录模板（复制填写，结论写入 docs/evidence/cos-20260924.md）

```yaml
decision: reuse-existing | dedicated-test      # 二选一
bucket: <name-appid>                            # 实际使用桶
region: <ap-xxx | eu-xxx | ...>
decided_at: "<Asia/Shanghai 日期时间>"
reason: >
  <若 reuse：说明现有桶无版本控制/CORS/生命周期冲突，poc/ 前缀可安全隔离；
   若 dedicated：说明哪一项冲突（例如现有桶已开版本控制且承载生产对象，不能
   用于全版本清理演练；或现有桶 CORS 不能加规则）>
operator: < initials / role，不写完整账号 >
existing_config_snapshot: >
  <变更前快照：版本控制状态、CORS 规则数、生命周期规则数、在途 multipart 情况。
   控制台截图存档于本地（不入仓库），此处文字摘录>
```

## 1. 版本控制（候选 B 硬前提；候选 A 视证明结果）

- [ ] 确认目标桶当前版本控制状态（未开启 / 已开启 / 已暂停）。
- [ ] **不可逆警告**：版本控制开启后**只能暂停、不能恢复"从未启用"状态**；暂停也不会删除
      已有版本。复用生产桶前必须确认所有调用方能接受"对象多版本"语义（合同 §10）。
- [ ] 专用测试桶：开启版本控制（候选 B §3.0 条件 4 与 §3.2 要求 worker 钉 versionId）。
- [ ] 记录开启时间（影响"历史版本清理"与账单口径）。
- 参考：https://intl.cloud.tencent.com/zh/document/product/436/19883

## 2. CORS 规则（精确 origin，含端口）

PoC 需要两条 origin：本地 dev 页面 `http://127.0.0.1:8765`（serve_poc.py 默认端口）与
生产 origin 占位符（真实值来自 `COS_POC_PROD_ORIGIN`，形如 `https://host:18445`）。

控制台 → 存储桶列表 → 目标桶 → 安全管理 → 跨域访问 CORS 设置 → 新增规则：

```json
[
  {
    "CORSRule": {
      "AllowedOrigin": "http://127.0.0.1:8765",
      "AllowedMethod": ["PUT", "GET", "POST", "DELETE", "HEAD"],
      "AllowedHeader": ["*", "content-length", "content-type", "x-cos-security-token"],
      "ExposeHeader": [
        "ETag",
        "Date",
        "Content-Length",
        "Content-Type",
        "x-cos-request-id",
        "x-cos-version-id",
        "x-cos-trace-id"
      ],
      "MaxAgeSeconds": 600
    }
  },
  {
    "CORSRule": {
      "AllowedOrigin": "{{COS_POC_PROD_ORIGIN}}",
      "AllowedMethod": ["PUT", "GET", "POST", "HEAD"],
      "AllowedHeader": ["*", "content-length", "content-type", "x-cos-security-token"],
      "ExposeHeader": [
        "ETag",
        "Date",
        "Content-Length",
        "Content-Type",
        "x-cos-request-id",
        "x-cos-version-id"
      ],
      "MaxAgeSeconds": 600
    }
  }
]
```

注意：

- **PoC 专用放宽**：dev 规则故意放行 GET/DELETE/HEAD，否则负向测试分不清
  "CAM 拒绝（预期 403）"和"浏览器 CORS 拦截"。PoC 结论产出后，生产 origin 的
  规则按合同 §5 收紧到实际需要的最小集合（PUT/POST + 必要 GET）。
- `ETag`/`x-cos-version-id` 必须在 ExposeHeader 里，浏览器才能读到（§3.0 条件 5、§5）。
- 控制台保存后**必须**用真实 OPTIONS 预检复核（浏览器 devtools 或
  `curl -X OPTIONS -H "Origin: ..." -H "Access-Control-Request-Method: PUT" https://<bucket>.cos.<region>.myqcloud.com/`）。
- 复用生产桶时：先导出现有规则备份，逐条核对无冲突（origin 重复会命中哪条由控制台合并逻辑决定，需实测）。

## 3. 生命周期规则（限定 `poc/` 前缀；仅兜底，不是容量控制器）

合同 §6.2：生命周期只作 7 天孤儿兜底，不能作为 10 GB 实时容量控制器；§6.3：
"配置未完成分块清理。碎片收费"。

- [ ] 新建生命周期规则，**范围限定前缀 `poc/`**（控制台：基础配置 → 生命周期），
      覆盖两类对象：
  - [ ] "删除过期对象碎片"（AbortIncompleteMultipartUpload）：`poc/` 前缀上传
        中（in-progress）multipart **7 天**后清理。
  - [ ] （可选，便于 PoC 收尾）非当前版本对象 7 天过期——**仅测试桶/测试前缀启用**；
        生产 `incoming/` 前缀的该规则在正式实现阶段按 §6.2/§6.3 另行设计。
- [ ] 确认规则创建时间与最小生效粒度（生命周期异步执行，不等价于满 7 天立即释放）。
- [ ] 复用生产桶时：确认新规则不作用于现有前缀；导出现有生命周期规则备份。
- 参考：https://cloud.tencent.com/document/product/436/56548

## 4. 用量 / 账单查询入口

- [ ] 控制台 → 存储桶列表 → 目标桶 → 基础配置 / 统计：当前存储量、对象数。
- [ ] 控制台 → 费用中心 → 账单管理 → 明细账单：按产品"对象存储 COS"筛选，
      记录外网下行流量（元/GB 口径 0.5 元/GB 基准，合同 §6.3）与请求费用行。
- [ ] 控制台 → 布告栏/消息中心：配置存储量与费用告警阈值（PoC 期间建议 ≥ 1 GB 与
      ≥ 10 元），告警本身不是硬停（§6.3）。
- [ ] 记录账单导出方式（每日明细 CSV），供 bench 结果与账单交叉核对。

## 5. 凭证与子账号准备（对象前缀隔离 ≠ 桶级配置隔离）

- [ ] 为 PoC 建独立子账号/密钥对（或使用指定的测试身份），只授予目标桶的相关权限；
      禁止使用主账号密钥跑脚本（合同 §10 第 3 条）。
- [ ] 该凭证用于：STS 签发（`sts_issuer.py`）、admin 审计/清理（`serve_poc.py`）、
      候选 A 自测（`presign_parts.py selftest`）、下载 bench。全部从
      `COS_POC_SECRET_ID`/`COS_POC_SECRET_KEY` 环境变量注入，不落文件。
- [ ] 桶级配置（版本控制/CORS/生命周期）由操作者在控制台完成——**不要**为脚本
      凭证授予这些配置权限（§10：桶级配置权限单独由操作者或管理身份持有）。
- [ ] PoC 结束后吊销该凭证，并在证据文件记录吊销时间。

## 执行记录（操作者填写）

| 项 | 状态 | 执行时间 (Asia/Shanghai) | 备注 |
|---|---|---|---|
| 0. 桶决策 | ☐ | | 结论 reuse/dedicated + 理由 |
| 1. 版本控制 | ☐ | | |
| 2. CORS（含 OPTIONS 复核） | ☐ | | |
| 3. 生命周期（poc/ 前缀） | ☐ | | |
| 4. 用量/账单入口 | ☐ | | |
| 5. PoC 凭证 | ☐ | | 建立时间；吊销时间事后补 |

### 2026-09-25 回填与待办（Agent 侧已核部分）

操作者已执行并经 Agent 复核的项目（evidence cos-20260924.md）：

- **1. 版本控制：已开启**（§7 探针 PUT 返回 x-cos-version-id，按 versionId HEAD/DELETE 均通过）。开启时间未记录——请在行内补填控制台显示的开启时间。
- **2. CORS：已配置**（§9：四个 origin——`http://127.0.0.1:8765` 与三个生产 origin 的 PUT 预检 + ETag 暴露均 200）。
- **5. PoC 凭证：已建立**（`PathTogether/.env.cos-poc`，600，git 排除）。**尚未吊销**——产品化继续使用，Phase 0 费用核对完成后吊销并记录时间。

Agent 无法代查、仍需操作者在控制台执行的项目（PoC 子账号 API 尝试已记录：GetBucketVersioning/CORS/Lifecycle 均 403 AccessDenied，GetBucketLocation 200，见 evidence §10.3）：

- [ ] **3. 生命周期（§4 表第 3 行）**：确认 `poc/` 前缀（及将来的 `incoming/` 前缀）的过期天数与"删除碎片/未完成 multipart 自动清理"规则的实际配置值，回填上行；该规则仅作 7 天兜底，不是 10 GB 实时控制器（合同 §6.2）。
- [ ] **4. 用量/账单（§4 表）**：按 evidence §10.4 的用量账本（上行约 2.4 GB、外网下行约 0.8 GB、无驻留存储）核对控制台用量统计与账单明细，回填实际计费数字；差异过大先查非 PoC 写入来源。
