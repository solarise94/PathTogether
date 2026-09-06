# viewer 编码基准报告

- baseline: {"label": "baseline-mc-tile-q95-444-noopt", "rgb": {"profile_id": "legacy", "quality": 82, "subsampling": 2, "optimize": false, "progressive": false}, "multichannel": {"profile_id": "legacy-mc-tile", "quality": 95, "subsampling": 0, "optimize": false, "progressive": false}}
- candidate: {"label": "candidate-viewer-v1", "rgb": {"profile_id": "native-standard-v1", "quality": 82, "subsampling": 2, "optimize": true, "progressive": false}, "multichannel": {"profile_id": "fluorescence-preserve-v1", "quality": 95, "subsampling": 0, "optimize": true, "progressive": false}}
- ROI 计数: {"rgb_rois": 60, "multichannel_rois": 57, "rgb_samples": 5, "multichannel_samples": 4}

## rgb/baseline
- n: 60
- bytes_median: 18625.5
- bytes_p95: 37512
- psnr_median: 44.06921676485628
- ssim_median: 0.9914621663970289
- edge_mae_median: 4.585703028321275
- weak_mae_median: 7.8232931726907635
- encode_ms_p95: 0.46445801854133606
- decode_ms_p95: 0.5585410399362445

## rgb/candidate
- n: 60
- bytes_median: 15478.0
- bytes_p95: 36792
- psnr_median: 44.06921676485628
- ssim_median: 0.9914621663970289
- edge_mae_median: 4.585703028321275
- weak_mae_median: 7.8232931726907635
- encode_ms_p95: 1.1159999994561076
- decode_ms_p95: 0.5602920427918434

## rgb/ratio_candidate_vs_baseline
- bytes: 0.8310112480201873
- edge_mae: 1.0
- ssim: 0.0

## multichannel/baseline
- n: 12
- bytes_median: 10783.0
- bytes_p95: 51370
- psnr_median: 62.7166927231782
- ssim_median: 0.9995606775739837
- edge_mae_median: 1.9173271173271171
- weak_mae_median: 1.3686781988643866
- encode_ms_p95: 0.7165409624576569
- decode_ms_p95: 0.6983750499784946

## multichannel/candidate
- n: 12
- bytes_median: 7267.5
- bytes_p95: 50542
- psnr_median: 62.7166927231782
- ssim_median: 0.9995606775739837
- edge_mae_median: 1.9173271173271171
- weak_mae_median: 1.3686781988643866
- encode_ms_p95: 1.789625035598874
- decode_ms_p95: 0.6846250034868717

## multichannel/ratio_candidate_vs_baseline
- bytes: 0.673977557266067
- edge_mae: 1.0
- ssim: 0.0
