# viewer 编码基准报告

- baseline: {"label": "baseline-legacy", "rgb": {"profile_id": "legacy", "quality": 82, "subsampling": 2, "optimize": false, "progressive": false}, "multichannel": {"profile_id": "legacy-mc-default-sampling", "quality": 90, "subsampling": 2, "optimize": false, "progressive": false}}
- candidate: {"label": "candidate-detail-q88", "rgb": {"profile_id": "native-detail-q88", "quality": 88, "subsampling": 0, "optimize": true, "progressive": false}, "multichannel": {"profile_id": "fluorescence-preserve-v1", "quality": 95, "subsampling": 0, "optimize": true, "progressive": false}}
- ROI 计数: {"rgb_rois": 55, "multichannel_rois": 0, "rgb_samples": 6, "multichannel_samples": 0}

## rgb/baseline
- n: 55
- bytes_median: 18435
- bytes_p95: 30257
- psnr_median: 44.22722726531629
- ssim_median: 0.9911413443989605
- edge_mae_median: 3.9382613510520486
- weak_mae_median: 2.425913621262459
- encode_ms_p95: 0.45320799108594656
- decode_ms_p95: 0.5577079718932509

## rgb/candidate
- n: 55
- bytes_median: 28519
- bytes_p95: 47067
- psnr_median: 47.06801108235187
- ssim_median: 0.9941523414010529
- edge_mae_median: 2.6168384879725086
- weak_mae_median: 2.041214470284238
- encode_ms_p95: 1.8588339444249868
- decode_ms_p95: 0.8033749181777239

## rgb/ratio_candidate_vs_baseline
- bytes: 1.5470029834553838
- edge_mae: 0.6644654213396627
- ssim: 0.003010997002092397
