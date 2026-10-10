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
{"ok": true, "device": "cuda", "model": "sam2.1-hiera-b+"}
```

模型加载失败时 `ok=false` 且 HTTP 503。

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
  `too_few_points`。

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
| `CONTOUR_REFINE_EAGER_LOAD` | `1` | 启动即加载模型（测试置 0 改惰性） |
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
