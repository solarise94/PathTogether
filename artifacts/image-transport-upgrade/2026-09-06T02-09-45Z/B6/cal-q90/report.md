# viewer 编码基准报告

- baseline: {"label": "baseline-legacy", "rgb": {"profile_id": "legacy", "quality": 82, "subsampling": 2, "optimize": false, "progressive": false}, "multichannel": {"profile_id": "legacy-mc-default-sampling", "quality": 90, "subsampling": 2, "optimize": false, "progressive": false}}
- candidate: {"label": "candidate-detail-q90", "rgb": {"profile_id": "native-detail-q90", "quality": 90, "subsampling": 0, "optimize": true, "progressive": false}, "multichannel": {"profile_id": "fluorescence-preserve-v1", "quality": 95, "subsampling": 0, "optimize": true, "progressive": false}}
- ROI 计数: {"rgb_rois": 55, "multichannel_rois": 0, "rgb_samples": 6, "multichannel_samples": 0}

## rgb/baseline
- n: 55
- bytes_median: 18435
- bytes_p95: 30257
- psnr_median: 44.22722726531629
- ssim_median: 0.9911413443989605
- edge_mae_median: 3.9382613510520486
- weak_mae_median: 2.425913621262459
- encode_ms_p95: 0.4507920239120722
- decode_ms_p95: 0.5038329400122166

## rgb/candidate
- n: 55
- bytes_median: 32463
- bytes_p95: 51573
- psnr_median: 47.648455590470185
- ssim_median: 0.9952666321647963
- edge_mae_median: 2.393964562569214
- weak_mae_median: 1.8238290398126464
- encode_ms_p95: 1.8321250099688768
- decode_ms_p95: 0.7603330304846168

## rgb/ratio_candidate_vs_baseline
- bytes: 1.7609438567941416
- edge_mae: 0.6078734622144113
- ssim: 0.004125287765835761
