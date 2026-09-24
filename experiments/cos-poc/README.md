# COS 前端直传 PoC（Phase 0 准备）

本目录是 [COS 执行合同](../../docs/cos-direct-upload-audit-plan.md) **Phase 0（真实
PoC 与授权裁决）** 的工具链。当前状态：**准备完成、未执行真实 COS 调用**——腾讯云
凭证尚不存在，所有依赖外部输入的步骤以 `blocked_external_input` 记录于
[证据文件](../../docs/evidence/cos-20260924.md)。绝不以 mock/硬编码密钥替代。

## 范围与合同对应

| 组件 | 合同条款 |
|---|---|
| 候选 B：`web/poc_b.{html,js}` + `tools/sts_issuer.py` + `policies/cam-single-key.json` + `vendor/`（本地托管锁版本 SDK） | §3.2 候选 B；§3.0 条件 1/2/3/5 判定 |
| 候选 A：`tools/presign_parts.py` | §3.1 候选 A；§3.0 A-1..A-4；B 失败后才执行 |
| 三段吞吐：`tools/cos_download_bench.py`（COS→服务器腿）+ 本文测量规程（另两腿） | §7 阶段 0"三段吞吐"；分流调研 §0 校准规则 1 |
| 测试文件：`tools/make_test_files.py` | §10 前置输入"可复现随机文件生成方法" |
| 控制台准备：`ops/console-checklist.md` | §10"PoC 环境边界"；§5 CORS；§6.2/§6.3 生命周期 |
| dev 服务：`tools/serve_poc.py` | §3.2 getAuthorization 服务端；生产 origin 验证走获准发布路径，不在本目录 |

不在范围：产品代码、`deploy/`、`app.py`、`upload_guard.py`、`static/`、`tests/`
一概不改；Phase 1+ 的任何实现。

## 运行前提

- Python ≥ 3.10（本机验证用 3.14.4）；node ≥ 18（仅用于 `node --check` 语法校验，可选）。
- 隔离虚拟环境（**不碰产品 requirements**）：

```bash
cd experiments/cos-poc
python3 -m venv .venv          # 若报 ensurepip 不可用：python3 -m venv --without-pip .venv
                                # 再 .venv/bin/python /tmp/get-pip.py（bootstrap.pypa.io）
.venv/bin/pip install -r requirements-poc.txt   # qcloud-python-sts==3.1.6, requests==2.34.2, numpy==2.5.3
```

依赖清单钉死版本且与产品依赖完全隔离；`serve_poc.py`/`make_test_files.py`
（无 numpy 时）/`presign_parts.py` 仅用标准库。

## 环境变量（唯一凭证入口；一律不落文件）

| 变量 | 必填 | 说明 |
|---|---|---|
| `COS_POC_SECRET_ID` | 执行时 | PoC 子账号密钥 Id（ops/console-checklist.md §5） |
| `COS_POC_SECRET_KEY` | 执行时 | PoC 子账号密钥 Key；禁止 export 到 shell 历史/文件 |
| `COS_POC_BUCKET` | 执行时 | `name-appid` 形如 `mybucket-1250000000` |
| `COS_POC_REGION` | 执行时 | 如 `ap-guangzhou` |
| `COS_POC_PREFIX` | 否 | 测试前缀，默认 `poc/`（必须形如目录前缀） |
| `COS_POC_PROD_ORIGIN` | 生产 origin 验证时 | 协议+域名+端口完整，如 `https://host:18445` |
| `COS_POC_ADMIN_TOKEN` | 否 | serve_poc.py admin 端点附加口令（在 127.0.0.1 绑定之上） |

缺失时所有脚本统一报 `blocked_external_input` 并列出缺项、退出码 2；不猜测、不降级、
不 mock。

## 运行顺序

### 0. 本地自检（无需凭证，已执行，见证据文件）

```bash
python3 -m py_compile tools/*.py
node --check web/poc_b.js
python3 tools/make_test_files.py --sizes 1048576 --out-dir data/smoke
python3 tools/make_test_files.py --sizes 1048576 --out-dir data/smoke --verify
python3 tools/serve_poc.py --port 8765 &   # curl http://127.0.0.1:8765/ 应返回页面
```

### 1. 操作者控制台准备（ops/console-checklist.md）

先完成"复用桶 vs 专用测试桶"决策记录，再依次配置版本控制、CORS、生命周期、
用量/账单入口、PoC 子账号。**版本控制开启后只能暂停**，复用生产桶前必须完成决策记录。

### 2. 生成测试文件（随机内容，禁止医疗数据）

```bash
.venv/bin/python tools/make_test_files.py --sizes 300000000,500000000          # 必测
.venv/bin/python tools/make_test_files.py --sizes 2000000000                   # 条件追加
.venv/bin/python tools/make_test_files.py --sizes 9499000000 --out-dir data/edge   # 边界（可选）
.venv/bin/python tools/make_test_files.py --out-dir data/testfiles --verify     # manifest 校验
```

所有尺寸是十进制整数（D3：`300000000`，不是 `300*1024*1024`）。manifest 记录尺寸、
方法、种子与 SHA-256。

### 3. 候选 B（先做；合同 §3.0"先做 B"）

```bash
# 3a. dry-run 检查将发给 STS 的策略（无网络调用）
COS_POC_BUCKET=... COS_POC_REGION=... python3 tools/sts_issuer.py --dry-run

# 3b. 启动 dev 服务（浏览器打开 http://127.0.0.1:8765/）
.venv/bin/python tools/serve_poc.py --port 8765

# 3c. 页面操作顺序：
#   分配 key → 上传正向用例 → 逐个负向开关（跨 key/GetObject/Delete/ListBucket 均
#   预期 403）→ 同 key 重复写 → 在 STS TTL 内点“占用审计”核对 版本+碎片 ≤ 预约字节
#   → Abort → 全版本清理。导出脱敏结果 JSON，摘录进证据文件。
```

**§3.0 条件 3 硬门槛**：审计必须在凭证有效期内、重复写发生后立即执行；
`grand_total_bytes > reserved_bytes` 即判 B 失败，随后清理只是兜底、不能改判。

### 4. 候选 A（仅当 B 任一硬条件失败，记录证据后）

```bash
.venv/bin/python tools/presign_parts.py plan --size 500000000            # 十进制分块计划
.venv/bin/python tools/presign_parts.py selftest --test-unbound          # 负向全套（真实 COS）
#   --fast 跳过 ~70s 过期重放等待；结果自动追加进证据文件
```

### 5. 三段吞吐（§7 阶段 0）

```bash
# COS→服务器腿（服务器直连 endpoint，不绕 frp）
.venv/bin/python tools/cos_download_bench.py --key <uploaded-key> \
    --manifest data/testfiles/manifest.json --repeats 3
```

浏览器→COS 与浏览器→平台/frp 两腿按下面"三段吞吐测量规程"人工执行，统一用
`tools/evidence_log.py` 的格式摘录进证据文件。

## 三段吞吐测量规程

同一测试文件（同一 SHA-256），三腿分别记录：attempts、failures、bytes、seconds、
Mbps（十进制，Mbps = bytes × 8 / seconds / 10^6）、测试时间与网络环境。

1. **浏览器→COS**：dev 页面上传同一文件，取 SDK `onProgress` 从 0→100% 的墙钟时间
   （或 devtools Network 面板该批 PUT 的总时长），字节 = 文件尺寸。记录失败分块数。
   需在获准网络环境（大陆上传网）执行。
2. **浏览器→平台/frp**：用**现有 V2 分块接口**（`PUT /api/uploads/{id}/chunk`）。
   辅助计时：浏览器 devtools 控制台执行（仅测量，不改产品代码）：
   `const t0=performance.now(); await fetch(...)` 逐块计时，或 HAR 导出后取
   chunks 总时长；服务器侧核对 `UPLOAD_HOURLY_REQUEST_LIMIT` 未触顶。此腿必须在
   获准的低峰窗口、获准的发布入口执行（§10），窗口日期时间由操作者提供，禁止臆造。
3. **COS→服务器**：`cos_download_bench.py`（自动写入证据）。

## 脱敏纪律（§10）

- SecretId/SecretKey/STS Token/签名 URL：不写文件、不打日志、不进截图/HAR 导出。
  所有脚本从环境变量读取；`sts_issuer.py --print-shape` 只输出长度与时间戳。
- `serve_poc.py` 请求日志只记 method+path（query 一律剥离）。
- 证据文件里 bucket 名与 region 可以出现；request id / 错误码 / 字节数 / Mbps 可以出现。
- `evidence_log.append_block` 有二次拦截（拒绝写入含 `q-signature=`/`AKID…` 等形状的文本），
  但这只是兜底——上游纪律是第一道防线。
- 测试内容只允许随机字节；禁止上传任何真实医疗数据。

## 目录结构

```
experiments/cos-poc/
├── README.md                 # 本文件
├── VENDORED.md               # 锁版本 SDK 记录（版本/来源/SHA-256/MIT）
├── requirements-poc.txt      # PoC 独立依赖（钉版本）
├── vendor/                   # cos-js-sdk-v5 v1.10.1 dist + LICENSE（不得手改）
├── policies/cam-single-key.json   # 单 key 写权限 CAM 模板（含注释与 §3.0 映射）
├── tools/
│   ├── poc_config.py         # 共享环境变量配置（脱敏 describe）
│   ├── cos_xml_signing.py    # COS XML API 签名（官方算法移植，已交叉验证）
│   ├── evidence_log.py       # 统一证据块格式 + 脱敏拦截
│   ├── make_test_files.py    # 随机测试文件生成 + manifest + verify
│   ├── sts_issuer.py         # 单 key STS 签发（候选 B）
│   ├── serve_poc.py          # dev 静态 + /api/poc-sts + admin audit/cleanup
│   ├── presign_parts.py      # 候选 A：plan/sign/selftest（负向全套）
│   └── cos_download_bench.py # COS→服务器腿计时 + SHA-256 交叉核对
├── web/poc_b.html, poc_b.js  # 候选 B 测试页（凭证仅内存）
├── ops/console-checklist.md  # 操作者控制台清单 + 桶决策记录模板
└── data/                     # 生成产物（gitignore）
```
