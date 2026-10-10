# contour-refine

SAM2.1（hiera base+）box-prompted 轮廓精修微服务。HistoPilot 侧的 LLM
（`draw_suspicious_region`）在 WSI 上画出的多边形定位准但轮廓粗；本服务用
LLM 形状的 bbox 做 box prompt，让 SAM2 勾出腺体/导管/肌束边界，并按
分数/面积比闸门决定接受（扩散区域拒绝、保留 LLM 原轮廓）。算法来自离线
原型（gate_sam2.py / refine_proto.py）。

PathTogether 平台侧入口：`POST /api/plugin/v1/slides/<slide>/contours/refine`
（读取 crop、映射坐标、调用本服务，见 PathTogether app.py）。

## API

### `GET /healthz`

```json
{"ok": true, "device": "cuda", "model": "sam2.1-hiera-b+", "pid": 7}
```

只读本 **worker 进程**的就绪状态（不触发加载）：模型未加载/加载失败 →
`ok=false` 且 HTTP 503。`pid` + `device` 用于确认模型确实加载在本 worker
（见下「模型加载与 fork」），不能证明 master 或其它进程的 GPU 推理可用。

### `POST /v1/refine`

请求（`application/json`）：

```json
{
  "image_png_b64": "<base64 PNG，RGB，长边 ≤ 2048>",
  "polygon": [[120.0, 80.0], [260.0, 90.0], [250.0, 210.0], [110.0, 200.0]]
}
```

- `polygon`：**crop 像素坐标**，3..500 个有限数值点（调用方负责 level-0 →
  crop 的映射）；
- 请求体 ≤ 8 MB；设置了 `CONTOUR_REFINE_TOKEN` 时须带 `X-Refine-Token` 头
  （常数时间比较，失败 401）。

响应（业务结果一律 HTTP 200；校验失败 400 `{error}`）：

```json
{
  "accepted": true,
  "reason": null,
  "score": 0.93,
  "area_ratio": 0.87,
  "points": [[131, 84], [258, 96], [244, 205], [118, 197]],
  "model": "sam2.1-hiera-b+",
  "seconds": 0.62
}
```

- `points`：crop 像素整数点（拒绝时为 `null`，调用方保留 LLM 原轮廓）；
- `reason`（拒绝时）：`low_score` / `area_ratio` / `empty_mask` /
  `too_few_points`；
- 排队上界：单模型实例推理串行（进程内互斥锁）；另一推理进行中时最多等
  `CONTOUR_REFINE_QUEUE_WAIT_S`（默认 2 s），超时 → **HTTP 503
  `{"error": "busy"}`**（调用方 PT 映射为 `refined:false` reason `busy`，
  不无限排队——PT 客户端 8 s 超时，已放弃的请求不能在后台继续堆积计算）。

算法（`refine.py`，纯逻辑、可脱离 torch 单测）：

1. box = 多边形 bbox → SAM2；
2. mask = 预测 mask ∩（多边形 mask 膨胀 `r = max(3, 0.10*sqrt(area))`）；
3. 最大 8-连通分量 + 5x5 开运算；
4. `ratio = mask_area / polygon_area`；接受 iff `score ≥ 0.75` 且
   `0.3 ≤ ratio ≤ 1.3`；
5. 最大外轮廓 → `approxPolyDP(eps = max(1.0, 0.006*perimeter))`；点数 > 500
   时逐级放大 epsilon 收敛。

## 构建 / 运行（podman + NVIDIA CDI）

```bash
podman build -t contour-refine .

podman run -d --name contour-refine \
  --device nvidia.com/gpu=all \
  -v /path/to/seg-weights:/models:ro \
  -p 8970:8970 \
  contour-refine
```

- 权重**不进镜像**，只读挂载到 `/models`（缺省取
  `/models/sam2.1_hiera_base_plus.pt`）；
- 需要 NVIDIA CDI（`nvidia-ctk cdi generate`）；无 GPU 时自动回落 CPU
  （单次推理显著变慢，冒烟可用）。

### 模型加载与 fork（勿回退 --preload）

镜像命令 `gunicorn --config gunicorn.conf.py app:app` **不用 `--preload`**
（`gunicorn.conf.py` 显式 `preload_app = False`）：app 导入与 SAM2/CUDA 初始化
都发生在 fork 之后的 worker 进程内（import 期 eager load +
`post_worker_init` 幂等 warm-up，`CONTOUR_REFINE_EAGER_LOAD=0` 部署下也保证
worker 启动即加载）。CUDA 上下文在 gunicorn master 创建后被 fork 继承违反
PyTorch 的 poison-fork 约束（GPU 模式下必须避免）。单 worker 不变；`python
app.py` 直跑（开发）行为不变。

**GPU 验收**（只看 `/healthz` 不足，需真实推理）：

1. 在 GPU 机器上用最终镜像运行（如上），`curl /healthz` 确认
   `"ok": true` 且 `"device": "cuda"`、`pid` 为 worker 进程；
2. 真实 `POST /v1/refine`（curl 自检的 req.json）返回 `accepted` 结果、
   `seconds` 量级正常（GPU 单次 ≪1 s）；
3. 超时回退：把 PT 侧 `CONTOUR_REFINE_TIMEOUT_S` 临时调小（或停服务）发起
   细化，确认 PT 回 `refined:false`（`service_error` / `busy`）且保留模型
   原轮廓，服务端无请求堆积。

curl 自检：

```bash
curl -s http://127.0.0.1:8970/healthz
python3 - <<'PY' > /tmp/req.json
import base64, io, json
from PIL import Image
img = Image.new("RGB", (256, 256), (200, 190, 180))
b = io.BytesIO(); img.save(b, "PNG")
print(json.dumps({"image_png_b64": base64.b64encode(b.getvalue()).decode(),
                  "polygon": [[60, 60], [196, 60], [196, 196], [60, 196]]}))
PY
curl -s -X POST http://127.0.0.1:8970/v1/refine \
  -H 'Content-Type: application/json' --data @/tmp/req.json
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SAM2_CHECKPOINT` | `/models/sam2.1_hiera_base_plus.pt` | 权重路径（挂载点内） |
| `SAM2_CONFIG` | `configs/sam2.1/sam2.1_hiera_b+.yaml` | SAM2 模型配置名 |
| `CONTOUR_REFINE_TOKEN` | 未设置 | 设置后强制 `X-Refine-Token` 共享密钥 |
| `CONTOUR_REFINE_QUEUE_WAIT_S` | `2` | 推理排队等待上界（秒）；超时 503 `busy` |
| `CONTOUR_REFINE_EAGER_LOAD` | `1` | 启动即加载模型（worker 内；测试置 0 改惰性） |
| `CONTOUR_REFINE_MAX_IMAGE_EDGE` | `2048` | 入参图像长边上限（px） |
| `CONTOUR_REFINE_MAX_BODY_BYTES` | `8388608` | 请求体上限（字节） |
| `CONTOUR_REFINE_SCORE_MIN` | `0.75` | 接受分数下限 |
| `CONTOUR_REFINE_RATIO_MIN` / `_MAX` | `0.3` / `1.3` | 面积比区间 |
| `CONTOUR_REFINE_DILATION_FRAC` / `_MIN_RADIUS` | `0.10` / `3` | 泄漏容忍膨胀半径 |
| `CONTOUR_REFINE_SIMPLIFY_EPS_MIN` / `_FRAC` | `1.0` / `0.006` | approxPolyDP epsilon |
| `CONTOUR_REFINE_MAX_POINTS` | `500` | 输出点数上限 |
| `PORT` | `8970` | 仅 `python app.py` 直跑时生效（生产走 gunicorn） |

平台侧对应变量（PathTogether）：`CONTOUR_REFINE_URL`、`CONTOUR_REFINE_TOKEN`、
`CONTOUR_REFINE_TIMEOUT_S`（见 PathTogether README）。

## 测试

独立 venv（torch 2.x CPU + sam2 + opencv + flask + pytest）：

```bash
cd services/contour-refine
/home/solarise/pytest-tmp/seg-venv/bin/python -m pytest -q tests
```

opt-in 真实模型冒烟（读真实 WSI crop，CPU 约几十秒）：

```bash
SAM2_CHECKPOINT=/home/solarise/pytest-tmp/seg-weights/sam2.1_hiera_base_plus.pt \
  /home/solarise/pytest-tmp/seg-venv/bin/python -m pytest -q tests \
  -k real_model_duct_smoke -s
```

注意：本目录测试**不属于** PathTogether 主 pytest 套件（pytest.ini
`testpaths = tests` 已排除 `services/`），必须在带 torch/sam2 的 venv 里跑。
