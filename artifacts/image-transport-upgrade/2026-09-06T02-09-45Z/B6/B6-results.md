# B6 基准结果汇总（真实样本；运行时 Python 3.12.2 / Pillow 12.3.0 / jpeg 6.2 / openslide 1.4.6 / tifffile 2024.5.22）

样本（homePC 授权样本，已拷贝至套件根 bench-samples/，不在 git 内）：
- 真实 RGB HE SVS ×5：1-NC肠(1) / 2-MC肠(1) / 3-PC肠(1) / 4-LD肠(1) / 6-HD肠
- 真实多色荧光 OME-TIF ×1：E04260806007-LZM-Control（6C，47031×47104）；第 2 张 WKL 拷贝中
- 确定性合成极端 ×3（synthetic- 前缀，单独分表）：8ch uint16 / uint8 / float32
- ⚠ 荧光真实样本 2 张 < 任务书要求的 3 张 → G7 荧光样本量项 blocked（不用合成图充数）

## A. 图像级编码基准（scripts/benchmark_viewer_encoding.py，ROI 级参考对照）
RGB（60 ROI / 5 真实样本，候选=standard q82/4:2:0/optimize vs 基线=legacy 同 q 不优化）：
- bytes 中位数比 **0.831**（-17%）；PSNR/SSIM/edge MAE 比完全一致（optimize 只改熵编码，解码像素逐位相同）
精细标定（§7.2 阶梯 84→86→88→90，均 4:4:4+optimize vs 标准 q82/4:2:0；55-60 真实 ROI）：
| q | bytes 比 | edge MAE 改善 | SSIM Δ | 判定 |
|---|---|---|---|---|
| 84 | **1.231** ✓ | **16.4%** ✓(≥10%) | +0.00102 ✓ | **选定** |
| 86 | 1.371 ✗ | 25.4% | +0.00197 | 超预算 |
| 88 | 1.547 ✗ | 33.6% | +0.00301 | 超预算 |
| 90 | 1.761 ✗ | 39.2% | +0.00413 | 超预算 |
→ **最终 detail 配置固定 q84 / 4:4:4 / optimize**（VIEWER_DETAIL_CALIBRATED_QUALITY=84）
荧光 preserve（真实 LZM 12 ROI，候选=preserve q95/4:4:4/optimize vs 基线=旧 MC tile 同 q 同采样不优化）：
- bytes 比 **0.674**（-33%）；edge MAE 比 1.0、SSIM Δ=0（解码像素逐位一致）✓
- 合成极端（8ch/uint8/float）单独分表见 cal-mc/report.json rows（synthetic=true，未混入真实平均值）
荧光 thumbnail（真实 LZM 12 ROI，旧 save(q90,默认采样=4:2:0) vs fluorescence-thumb-v1 q95/4:4:4/opt）：
- 实际抽样 4:2:0→**4:4:4** ✓；彩色边缘 MAE 中位数 4.445→**1.993**（-55%）✓；bytes 4061→5264（thumbnail 本身 <6KB，
  增量已计入整条轨迹）

## B. 浏览器轨迹基准（真实 Flask+OpenSlide+嵌入 PG；Chromium；CDP Network 全量字节；
   无 route mock；完成判定=请求静默 900ms；1440×900；3 次配对重复取中位数）
标准 vs 旧版（同轨迹，总传输含瓦片/缩略图/info/dzi/render-context）：
| 条件 | 基线（旧代码） | 候选（新代码） | 比 | 预算 ≤1.10 |
|---|---|---|---|---|
| DPR=1 | 378,526 B | **318,039 B** | **0.840** | ✓ |
| DPR=2 | 1,449,750 B | **1,252,538 B** | **0.864** | ✓ |
荧光 preserve vs 旧版（含通道切换相位；真实 LZM 6 通道）：
- 候选 4,893,033 B vs 基线 5,126,014 B = **0.954** ✓（全部 tile 走 fluorescence-preserve-v1）
精细（§7.2 浏览器预算 ≤1.35）：
- 同轨迹"偏好即精细"（q84）：459,716 B vs 标准 318,039 B = **1.445 ✗ 超预算**
- ⚠ 偏差如实记录：图像级 ROI 口径 q84=1.231 达标；浏览器整轨迹口径 q84=1.445 超预算
  （超预算集中于概览层瓦片：4:4:4 对低倍平滑瓦片压缩损失更大）。任务书阶梯下限 84
  已是最低候选 → 按 §7.2"候选均失败时如实 blocked"，**G7 精细档浏览器预算项记 blocked**，
  不重命名不算成功；质量/预算取舍需产品决策后再定。
- 中途切换路径（先标准后点精细）：total 789,590 B（含两套瓦片重复获取，仅作参考非验收口径）
- 状态码：候选轨迹 200/304 混合（304=ETag 重验证，无 body）；409=0（修复 fp 同源后）
- 5xx：0

## C. 可复现命令
- 图像基准：`python scripts/benchmark_viewer_encoding.py --manifest <m> --output-dir <o> --baseline-config <c1> --candidate-config <c2>`
- manifest：`--generate-manifest --samples bench-samples --out manifest.json`（seed 固定）
- 轨迹：`node B6/run_trajectory.mjs --base http://127.0.0.1:8931 --label L --dpr 1|2 --repeats 3 --slide S [--start-detail|--detail|--channel-switch]`
- 轨迹服务器：`python B6/trajectory_server.py --port 8931 --repo <checkout> --upload-dir <dir> --pgdata <dir>`
- 荧光 thumbnail：`python B6/thumb_bench.py --manifest manifest.json --sample <id>`
