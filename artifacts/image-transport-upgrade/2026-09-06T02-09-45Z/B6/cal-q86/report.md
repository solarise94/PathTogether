# viewer 编码基准报告

- baseline: {"label": "baseline-legacy", "rgb": {"profile_id": "legacy", "quality": 82, "subsampling": 2, "optimize": false, "progressive": false}, "multichannel": {"profile_id": "legacy-mc-default-sampling", "quality": 90, "subsampling": 2, "optimize": false, "progressive": false}}
- candidate: {"label": "candidate-detail-q86", "rgb": {"profile_id": "native-detail-q86", "quality": 86, "subsampling": 0, "optimize": true, "progressive": false}, "multichannel": {"profile_id": "fluorescence-preserve-v1", "quality": 95, "subsampling": 0, "optimize": true, "progressive": false}}
- ROI 计数: {"rgb_rois": 55, "multichannel_rois": 0, "rgb_samples": 6, "multichannel_samples": 0}

## rgb/baseline
- n: 55
- bytes_median: 18435
- bytes_p95: 30257
- psnr_median: 44.22722726531629
- ssim_median: 0.9911413443989605
- edge_mae_median: 3.9382613510520486
- weak_mae_median: 2.425913621262459
- encode_ms_p95: 0.4376251017674804
- decode_ms_p95: 0.5131249781697989

## rgb/candidate
- n: 55
- bytes_median: 25266
- bytes_p95: 43598
- psnr_median: 46.50757625083783
- ssim_median: 0.9931066332219428
- edge_mae_median: 2.9385382059800667
- weak_mae_median: 2.2101873536299763
- encode_ms_p95: 1.767750014550984
- decode_ms_p95: 0.6366660818457603

## rgb/ratio_candidate_vs_baseline
- bytes: 1.3705451586655817
- edge_mae: 0.7461511423550089
- ssim: 0.001965288822982303
