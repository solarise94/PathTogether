# PathSegmentor Measure（测量插件 PoC）

把 PathSegmentor 语义分割模型作为 PathTogether 插件能力（`segment_region`）
暴露给 HistoPilot agent：对用户切片的一个有界 ROI 做目标分割，返回面积 /
近似计数 / 尺寸分布等**小型 JSON 摘要**（无 mask、无 base64 图像）。

链路（docs/measurement-models-homepc-implementation.md §2）：

```text
HistoPilot agent
  → PathTogether /api/plugin/v1/dispatch/dev.pathsegmentor.measure/segment_region
  → 本插件后端（本目录 backend/app.py，默认 127.0.0.1:8062）
      → PathTogether region 端点（X-Delegated-Read-Token 委托读凭证读 ROI）
      → PathSegmentor 推理服务（PATHSEGMENTOR_URL，GPU 留在 homePC）
  → ≤64KiB 摘要原路返回 agent
```

## 结构

- `manifest.json`：`provides[0] = segment_region`（read-only、
  `agent_exposed: true`、`timeout_ms: 55000`）；`ui` 为最小合法占位（schema
  必填）；manifest 内容由 `plugins/source-policy.json` 显式 sha256 pin。
- `backend/app.py`：插件后端。**强制校验** `X-Dispatch-Principal`（平台
  附加）与 `X-Delegated-Read-Token`（平台 dispatch 在 agent token / 用户实时
  权限 / 插件启用全部通过后签发的短时只读凭证，TTL ≤120s，绑定
  installation / user / slide_id / revision / capability / ROI 上限）——直连
  调用不受理。健康检查 `GET /healthz`；能力端点
  `POST /capabilities/segment_region`。
- `ui/main.js`：占位（本插件重点是服务端能力）。

## 能力契约

### segment_region（PathSegmentor，语义分割——区域/面积，不是计数器）

参数（level-0 整数 bbox + 受控文本提示）：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `x`,`y`,`w`,`h` | integer | level-0 bbox（`0<=x,y`、`0<w,h`；`w*h` ≤ 平台 ROI 上限，默认 4096² 像素，超限被平台 403 roi_over_cap 拒绝） |
| `object` | string | 要分割的目标：`nuclei` / `lymphocytes` / `tumor cells` / `gland` 等 |
| `region` | string? | 可选组织/器官语境提示 |
| `threshold` | number? | 可选前景阈值（0..1） |

返回（摘要，恒 ≪64KiB）：`analysis_id`、`model{id,sha256,code_rev}`、
`prompt_used`、`object/region_hint/threshold`、`slide/slide_id/revision`、
`source_bbox`、`rendered{src,width,height,read_level}`、`mpp`、`foreground`
（level-0 像素面积、ROI 面积占比、µm²——MPP 缺失时仅像素）、
`approx_object_count`（**语义分割近似计数**：`connected_components` 连通域
计数恒给出；`estimated_count_split` 为距离变换+分水岭拆分估计，仅当
`objects_resolved` ≥ 0.6 时给出，否则 null 并附 `mask_is_region_not_instances`
警告——mask 覆盖成片组织区域/细胞带时拆分会发明伪目标）、
`size_distribution`、`components_top`（≤20 个最大连通域的 level-0
bbox/质心，可用来标示例）、`timings_ms`、`warnings`。

平台回调按 **slide_id**（dispatch 转发，取自签名 token claim）走 by-id
slide-info / region 路由；仅名（legacy 行）回落 by-name。输出分辨率目标
**0.25 µm/px**（`PT_PSM_TARGET_MPP` 可调），且永不上采样超过切片原生
mpp（输出像素 ≤ level-0 源像素）。平台 404（切片不存在/不可读）由后端
归一为 `slide_unavailable`；dispatch 侧插件 4xx 归一为
`capability_failed`（404 → 可重试 503 `capability_unavailable`），细分码
在 `error.details.plugin_code`——避免 HP 把 404 渲染成「插件已停用」。

推理服务 HTTP 合同（独立部署，见下）：`GET /readyz` →
`{"ready":true,"model_id","model_sha256","code_rev","device"}`；
`POST /v1/segment`，body `{"image_b64": PNG/JPEG base64（≤2048²、≤8MB、RGB）,
"object", "region"?, "threshold"?}` → 200 `{"model_id","model_sha256",
"code_rev","prompt_used","input_width","input_height","mask_png_b64"
（与输入同尺寸，255=前景）,"positive_pixels","threshold","timings_ms",
"warnings"}`；400 `{"error","message"}`；503 `{"error":"busy"|"not_ready"}`。
后端把 busy/not_ready/不可达/契约不符映射为明确的工具级错误。

### count_cells（Cellpose，实例分割计数——逐个细胞/核）

与 segment_region 的分工：**要某类目标的区域/面积/占比 → `segment_region`；
要逐个计数、密度、单个细胞面积 → `count_cells`**。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `x`,`y`,`w`,`h` | integer | level-0 bbox（同上，受平台 ROI 上限约束） |
| `resolution` | integer | **模型处理长边上限**固定枚举 `512/1024/2048`（缺省 2048=服务输入上限、每档最大视场；512/1024 为更快的显式选项，时间成本约随 option² 增长；平台 regions 路由允许 max_long_edge ≤4096）。ROI 按 Cellpose 的实际处理尺度渲染（见下），模型处理长边（含内部放大）超过该选项时在读图前拒绝（`resource_limit`）——改选更大选项、把 ROI 拆成多个 tile，或在语义上适当增大 `diameter_um` |
| `diameter_um` | number? | 期望目标直径 µm，4–100（缺省 7=细胞核；整细胞 15–25）。决定处理尺度（d/30 µm/px）与可处理的最大视场 |
| `model` | string? | 可选 Cellpose 模型预设名（缺省服务端默认） |

返回（摘要）：`instance_count`（真实实例计数）、`density{per_mm2,
roi_area_mm2, basis}`（MPP 有效时；分母是实际成像 ROI——bbox 经边界夹取
后的 src）、`area_stats_px/area_stats_um2`（min/p25/median/p75/max）、
`instances_top`（≤20 个示例实例的 level-0 bbox/质心，非全量）、
`model{model_id,model_name,model_sha256,code_version}`、
`diameter_px_used`、`resolution`、`inference_input{mpp,scale,mpp_render}`、
`slide/slide_id/slide_revision`、`source_bbox`、`rendered`（含
`requested_long_edge`）、`mpp`、`timings_ms`、`warnings`。无 label map、
无 base64，恒 ≪64KiB。

**渲染与算力模型（R2 重设计；F2 处理长边合同）**：cpsam 内部按
`rescale=30/diameter_px` 把输入缩放到「每 diameter 30 px」的处理分辨率——
处理尺度 = `d/30 µm/px`（d=7 → 0.2333），处理边 = `30×视场/diameter`，
**与渲染分辨率无关**。因此渲染尺度取 `mpp_render = max(原生 MPP, d/30)`
（永不上采样超过原生），所需处理长边 = `ceil(L_level0 × 原生 / mpp_render)`。
`resolution` 是**模型处理长边**上限：预准入同时检查名义长边（逐轴 ceil）
与读后硬闸的预读镜像（平台 `_aspect_fit_size` 逐轴 round 的输出预测 + 两轴
均值 MPP + cellpose 4.2.1.1 的逐轴 `int(size×rescale)` 截断——同一几何下
与读后计算逐位一致，短轴取整变粗的窄长 ROI 也会在读前被拒），超过所选
选项或处理预算（8 Mi px）在读 ROI **之前**拒绝。渲染粗于处理尺度只会扔掉
真像素再让模型上采样（算力不变、信息变差），不做这种回退。原生粗于
d/30 时按原生渲染、模型内部上采样（附 warning，不增加信息）。
**每档最大视场 = resolution × diameter/30 µm，与切片 MPP 无关**；
d=7 时：**512→119.47 µm、1024→238.93 µm、2048→477.87 µm**
（原生 0.25 → 最长 477/955/1911 level-0 px；原生 0.12 → 995/1991/3982）。

**推理输入 MPP（R3）**：Cellpose 收到的是渲染后 ROI 的实际分辨率——
`inference_input.mpp = 原生 MPP × 实际渲染缩放`（由平台实际返回的
`src` 与输出 `width/height` 计算，非请求目标值；两轴均值与预读镜像同式，
保证预准入放行 ⇒ 读后硬闸必过；两轴各向差按平台渲染的取整物理容忍——
仅当输出不是「src 按长轴尺度的等比 round」（短轴偏差 >1 输出像素）时拒绝
`anisotropic_scale`，小 ROI 的合法取整（如 16 px 短边 ≈3% 相对差）照常
接受）。原生 MPP 仅用于结果映射（密度/µm²/坐标）。原生 MPP 缺失/无效时
按所选 `resolution` 长边渲染（rescale 1、处理尺寸 = 输入尺寸，恒在选项
与预算内），推理侧发送 JSON `null`（服务按模型缺省尺度计数并附 warning），
结果只给像素口径、不给密度/µm²（R4）。

**资源预算**：预算闸有三道，错误引导都按真实物理（缩小 ROI 拆 tile、增大
diameter_um 或改选更大 resolution）：读图前按 level-0 bbox + 原生 MPP +
diameter 推出模型处理长边与处理像素量（超选项 → 提示更大 resolution/
拆 tile/增大 diameter，附 `extra{max_roi_um, max_roi_level0_px, resolution,
diameter_um}`；超预算 → 提示缩小 ROI/增大直径）；读图后按**实际返回几何
与实际两轴均值推理 MPP**、以 cellpose 4.2.1.1 的真实缩放物理（逐轴
`int(size×rescale)` 截断，`CellposeModel.eval` models.py:267 与
`run_net` core.py:191 / `resize_image` transforms.py:747）再过一次硬闸：
模型处理长边 ≤ resolution 且总像素 ≤ 预算（正常路径与预读镜像一致，
此处纯防御；服务端同式准入仍在，双保险）。

计数服务合同（homePC）：`GET /readyz` → `{ready, model_id:"cellpose",
model_name, model_sha256, code_version, device}`；`POST /v1/count`，body
`{"image_b64", "mpp", "diameter_um"?, "model"?, "flow_threshold"?,
"cellprob_threshold"?}` → 200 `{"model_id","model_name","model_sha256",
"code_version","input_width","input_height","instance_count",
"diameter_px_used","areas_px":{min,p25,median,p75,max},"instances_top":
[{bbox,centroid,area_px}],"timings_ms","warnings"}`；400
`{"error","message"}`（含 `resource_limit`：图像头超限在解码前拒绝；合法
参数造成的内部放大超预算在进模型前拒绝）；503 `{"error":"busy"|
"not_ready"}`。错误映射与 PathSegmentor 相同（busy/not_ready/unreachable →
可重试 `capability_unavailable`，细分码在 `details.plugin_code`）。

## 环境变量

插件后端（`backend/app.py`）：

- `PT_PATHSEGMENTOR_MEASURE_PORT`：监听端口（默认 **8062**，须与 manifest
  `service.baseUrl` 一致）；
- `PT_PLATFORM_URL`：PathTogether 基地址（默认 `http://127.0.0.1:8000`）；
- `PT_INSTALLATION_ID` / `PT_INSTALLATION_SECRET`：本插件安装凭证（见下）；
- `PATHSEGMENTOR_URL`：PathSegmentor 推理服务基地址（默认
  `http://127.0.0.1:8971`）；
- `CELLPOSE_URL`：Cellpose 计数服务基地址（默认 `http://127.0.0.1:8972`）；
- `PT_PSM_TARGET_MPP`：PathSegmentor 目标输出分辨率 µm/px（缺省 **0.25**，
  永不超过原生）；count_cells 的模型处理长边上限为固定枚举
  512/1024/2048（缺省 **2048**，常量 `_COUNT_RESOLUTIONS`，无 env）；
  `PT_PSM_MAX_LONG_EDGE`
  （缺省 1024，segment_region 输出上限）/ `PT_PSM_MIN_COMPONENT_FRACTION`
  （默认 1e-4）/ `PT_PSM_MAX_COMPONENTS`（默认 20）；
- `PT_PSM_EXPECTED_DIAMETER_UM`（缺省 7.0，计数拆分的期望单目标直径）/
  `PT_PSM_OBJECTS_RESOLVED_MIN`（缺省 0.6，拆分 guard 阈值）。

平台侧（PathTogether，可选）：

- `PLUGIN_DELEGATED_READ_TTL_SECONDS`：委托读凭证 TTL（默认 120，钳 ≤120）；
- `PLUGIN_DELEGATED_ROI_MAX_PIXELS`：单凭证 level-0 bbox 像素上限
  （默认 4096²）。

## 本地安装与运行（demo 栈）

### 1) 起 PT + HP demo 栈

```bash
cd /path/to/histopilot-suite
./demo/run-demo.sh all          # 内嵌 PG + PathTogether :8000 + HistoPilot :8055
# owner 登录：demo-owner / $(cat .demo/run/owner-password)
```

AI 对话凭据：`.demo/ai.env`（0600，`CPA_BASE_URL` / `CPA_API_KEY` /
`CPA_MODEL`，不入 git）——把这三个值填进 owner 登录后的
「AI 配置（自有 API）」表单；不要把它们写进脚本或日志。

### 2) 安装插件 bundle 并取回安装凭证

```bash
# 登录拿 session cookie（CSRF 用 same-origin 表单流程；curl 示例）：
#   POST /login（demo-owner + 密码）→ flask session
curl -s -X POST http://127.0.0.1:8000/api/admin/plugins/install \
  -H 'Content-Type: application/json' \
  --cookie "session=<owner session>" -H "X-CSRF-Token: <csrf>" \
  -d '{"plugin": "pathsegmentor-measure"}'
# → 返回安装行，记下 installation_id

# 安装行创建时 secret 平台生成且不回显：轮换一次取明文（仅此一次返回）
curl -s -X POST \
  http://127.0.0.1:8000/api/admin/plugins/<installation_id>/rotate-secret \
  -H 'Content-Type: application/json' \
  --cookie "session=<owner session>" -H "X-CSRF-Token: <csrf>"
# → {"installation_id": "...", "secret": "<只显示这一次>"}
```

（PT 必须能读到本 bundle：仓库内 `plugins/pathsegmentor-measure/` 即可；
外部目录则设 `PLUGIN_BUNDLES_DIR` 指向其父目录。）

### 3) 打通 homePC 推理服务（本机经 SSH 隧道）

```bash
ssh -N -L 8971:127.0.0.1:8971 homepc &   # PathSegmentor
ssh -N -L 8972:127.0.0.1:8972 homepc &   # Cellpose（count_cells）
curl -s http://127.0.0.1:8971/readyz   # 期望 {"ready":true,...}
curl -s http://127.0.0.1:8972/readyz   # 期望 {"ready":true,"model_id":"cellpose",...}
```

### 4) 起插件后端（勿打印 secret）

```bash
cd /path/to/histopilot-suite/PathTogether
set -a; source /path/to/psm-install.env   # 只含 PT_INSTALLATION_ID/SECRET
set +a
PT_PLATFORM_URL=http://127.0.0.1:8000 \
PATHSEGMENTOR_URL=http://127.0.0.1:8971 \
CELLPOSE_URL=http://127.0.0.1:8972 \
/home/solarise/ZCodeProject/histopilot-suite/PathTogether/.venv/bin/python \
  plugins/pathsegmentor-measure/backend/app.py
# 监听 127.0.0.1:8062；curl -s http://127.0.0.1:8062/healthz
```

### 5) E2E

owner 登录 PT Viewer → 上传/打开切片 → 起 AI 会话（官方模式 run，注入
extra_tools）→ 模型按需要自选两个工具（工具名
`dev_pathsegmentor_measure__segment_region` /
`dev_pathsegmentor_measure__count_cells`）：
「这片区域有多少个 nucleus？bbox 取 (x,y,w,h)」→ count_cells；
「gland 在这片区域的面积占比」→ segment_region。验证点：

- 无权切片 / 插件停用 / ROI 超限 / 推理 busy → agent 收到明确工具级错误；
- 返回 JSON 无 mask/base64；重复同参调用可复核（revision 绑定）。

## 测试

```bash
cd /path/to/histopilot-suite/worktrees/pathsegmentor-plugin
python3 -m pytest tests/test_pathsegmentor_backend.py \
                   tests/test_delegated_read_credential.py -q
```

后端单测以 stub 推理服务验证机制（mask→level-0 映射、MPP 换算、连通域计数、
尺寸上限、错误映射）；平台安全测试覆盖凭证签发/复核全矩阵（越权、过期、
revision 变更、跨安装冒用、写通道不可用、HP 通道不变）。需要 PostgreSQL
（conftest 起内嵌 pgserver）。

## 已知边界（PoC）

- 首轮 owner/小范围启用；结果为研究用途，计数为语义分割近似值，需人工核对；
- 无 Viewer 掩码叠加（平台尚无测量资产接口），只返回数值摘要；
- dispatch 限流按（会话, 能力）计次、region 通道按像素预算——GPU 并发配额
  由推理服务自身串行化兜底。
