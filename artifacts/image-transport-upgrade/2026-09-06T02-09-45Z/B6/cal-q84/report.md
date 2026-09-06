# viewer 编码基准报告

- baseline: {"label": "baseline-legacy", "rgb": {"profile_id": "legacy", "quality": 82, "subsampling": 2, "optimize": false, "progressive": false}, "multichannel": {"profile_id": "legacy-mc-default-sampling", "quality": 90, "subsampling": 2, "optimize": false, "progressive": false}}
- candidate: {"label": "candidate-detail-q84", "rgb": {"profile_id": "native-detail-q84", "quality": 84, "subsampling": 0, "optimize": true, "progressive": false}, "multichannel": {"profile_id": "fluorescence-preserve-v1", "quality": 95, "subsampling": 0, "optimize": true, "progressive": false}}
- ROI 计数: {"rgb_rois": 55, "multichannel_rois": 0, "rgb_samples": 6, "multichannel_samples": 0}

## rgb/baseline
- n: 55
- bytes_median: 18435
- bytes_p95: 30257
- psnr_median: 44.22722726531629
- ssim_median: 0.9911413443989605
- edge_mae_median: 3.9382613510520486
- weak_mae_median: 2.425913621262459
- encode_ms_p95: 0.44595799408853054
- decode_ms_p95: 0.5485001020133495

## rgb/candidate
- n: 55
- bytes_median: 22692
- bytes_p95: 40592
- psnr_median: 45.935552108609286
- ssim_median: 0.9921582373969889
- edge_mae_median: 3.291005291005291
- weak_mae_median: 2.330737704918033
- encode_ms_p95: 1.6395830316469073
- decode_ms_p95: 0.6552080158144236

## rgb/ratio_candidate_vs_baseline
- bytes: 1.2309194467046378
- edge_mae: 0.8356492872485843
- ssim: 0.0010168929980284291
