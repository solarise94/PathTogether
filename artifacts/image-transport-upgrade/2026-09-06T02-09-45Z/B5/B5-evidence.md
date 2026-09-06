# B5 AI 与新旧版本兼容证据

## region 更新前后逐字节对拍（基线 HEAD 7583736 vs 实施 checkout）
- 方法：git worktree 基线（/tmp/pt-baseline-7583736）与主 checkout 各跑同一探针
  （tests/test_zz_region_hash_probe.py，探针文件不提交 git），同一 venv
  （Python 3.12.2 / Pillow 12.3.0 / jpeg 6.2 / tifffile 2024.5.22）、同一合成夹具。
- 结果（region-probe-baseline.json vs region-probe-current.json）：
  - region_rgb / region_mc_default / internal_ai_region_mc / internal_ai_region_rgb
    的 bytes_sha256、read_level、upsampled、encoder 元数据、render fingerprint、
    crop_png_sha —— **全部一致**（无哈希漂移）。
  - 唯一差异 render_token 签名：每会话随机 Flask secret 派生 HMAC（设计如此），
    非字节契约。
- 二进制/JSON 等价：HP `npm test` 1296 passed（含 http-client 二进制传输与
  JSON/base64 兼容、checkpoint gen1→gen2、request-assembler、overview-backfill）。
- 跨仓 contract：`PATHTOGETHER_REPO=... npm run test:contract` → 3 files / 41 passed。
- HP 生产代码变更：无（原则 §B5"HP 只需要新增回归测试"；既有契约未要求改动，
  未顺手迁移导数格式，DERIVATIVE_ENCODING_GENERATION 未动）。
