# 先转换后上传 —— 交接报告（阶段 1 + 五个新输入转换器）

状态：**已完成，全量门禁通过，待发布决策**。分支 `upload-convert-first`，
HEAD `c064d21`，工作区干净。本文汇总「先转换后上传」阶段 1（不新增转换器）
与随后新增的五个浏览器输入转换器（F4 SCN、F5 通用 TIFF/BigTIFF、F6 NDPI、
F7 VMS、F8 普通图片 BMP/JPEG）的最终状态、用户可见变化、验证情况与待决事项。
设计与规则详见 `docs/slide-tools/upload-convert-first-phase1.md`，各转换器
实测数据见 `docs/slide-tools/{scn,gtiff,ndpi,vms,raster}-adapter-report.md`。

提交脉络：阶段 1 四个提交（`fa2eb49` 后端、`4b01e74` 前端、`2d4b6f4` 浏览器
测试与文档、`9b7052b` 嗅探汇合点修正），基于 ux-formats `e043327`；阶段 1
审查三项发现（工作台 zip 误带声明 422、SVS 可被改名/zip 夹带绕过、嗅探内存
无界）已由 `2709f0c` 逐条修复并加回归测试；此后按 SVS 模式落地五个转换器
（F4 `382f41a`、F5 `9043489`、F6 `4090862`、F7 `180f36d`、F8 `0f35c60`），
最后两个提交 `81a15a9`/`c064d21` 修复全量门禁暴露的容器与测试环境问题。

## 1. 各格式状态与直传建议

下表数值直接取自服务端注册表目录行（`slide_format_registry.public_catalog()`，
报告撰写时用 venv Python 逐行打印核对）。

| 格式 | 扩展名 | 浏览器转换 | 直传当前 | 建议 | 理由 |
| --- | --- | --- | --- | --- | --- |
| Aperio SVS | .svs | 可用 | **关闭** | 维持关闭 | JPEG 编码 SVS 已可本机转换并经真实样本逐像素验收；JPEG2000 变体凭 `unconverted-variant:svs-jp2k` 声明仍可直传；已有内容级执行，改名 `.tif` 或 zip 夹带均被拒 |
| 3DHISTECH MRXS | .mrxs | 可用 | **关闭** | 维持关闭 | 必须整包（主文件 + 同名伴随目录），裸文件本就不可用；工作台「选择文件夹」交接已打通，zip 藏 MRXS 解包前拒绝 |
| KFB（明场）/ KFBF（荧光） | .kfb / .kfbf | 可用 | **关闭** | 维持关闭 | R1 起服务端转换分支已下线，浏览器转换是唯一路径；百度侧 KFBF 批次按构造不可建 |
| TIFF / BigTIFF | .tif / .tiff | 可用 | 开放 | 第二步再关 | 该行同时承载普通 TIFF（转换）与转换器导出的 BigTIFF（凭 `direct_class=converter-bigtiff` 直传）；「无声明的 Aperio SVS 内容」拒绝机制已在位，关闭「无声明的普通 TIFF」机制上可行，建议发布稳定后作为独立变更 |
| Hamamatsu NDPI | .ndpi | 可用 | 开放 | **建议关闭** | 明场整层 JPEG 变体已真实样本验收（真实样本 198 MB 转换成功，浏览器产物与原生逐字节一致）；JP2K/渐进等变体需仿照 svs-jp2k 增加变体声明例外后再关行 |
| Hamamatsu VMS | .vms | 可用 | 开放 | **建议关闭** | 整包（入口 + 同目录 tile JPEG）转换器已验收（compact 实跑 694 MB、与原生逐字节一致）；VMU 无转换器、单独成行不受影响 |
| Leica SCN | .scn | 可用 | 开放 | **建议关闭** | JPEG 明场 SCN 已真实样本逐像素零误差验收；荧光/非 JPEG 变体走暂时直传需保留例外 |
| 普通图片 | .bmp / .jpg / .jpeg | 可用 | 开放 | 维持开放 | 转换仅为重编码成 OME-TIFF（无物理标尺），对低价值图片关闭直传收益低、摩擦大 |
| OME-TIFF | .ome.tif / .ome.tiff | 不可用（本身即目标格式） | 开放（direct-upload） | 维持开放 | 嗅探确认后的直接上传类别，是所有转换产物的落点 |
| Hamamatsu VMU | .vmu | 不可用 | 开放（暂时直传） | 维持开放 | 尚无浏览器转换器，关闭后无处可去 |
| Ventana BIF | .bif | 不可用 | 开放（暂时直传） | 维持开放 | 转换器未立项（无可用公开样本，见 §4）；继续直接导入 |
| Sakura SVSlide | .svslide | 不可用 | 开放（暂时直传） | 维持开放 | 尚无浏览器转换器 |

关闭顺序建议：ndpi/vms/scn 一批（需配套变体声明词表的小改动）→ tif/tiff
第二步 → raster 不关。所有「关闭」均沿用阶段 1 的目录行机制：创建闸 422 +
worker 头级核验 + 声明例外词表。

## 2. 用户可见的行为变化

- **工作台**：拖放扩大到整页（document 级，文件夹拖入经 `webkitGetAsEntry`
  遍历）；导入抽屉改为大拖放区 +「选择文件」+「选择文件夹（MRXS）」，完整
  格式目录收进折叠区，每行带三种徽标之一（直接上传 / 本机转换后上传 / 暂时
  直接导入）。选择 KFB/KFBF/JPEG SVS/MRXS 不再建服务器转换任务，而是弹窗
  交接 `/tools/slides` 本机转换（MRXS/VMS 携带整文件夹成员）。
- **直传规则**：`.svs`（JPEG 编码）`.mrxs` `.kfb` `.kfbf` 不再受理直传；
  OME-TIFF 与转换器 BigTIFF 嗅探确认后带 `direct_class` 直接上传；其余格式
  照旧直传；zip 是运输容器、不携带声明。旧版缓存前端对 `.svs` 的上传会被
  422 明确拒绝。工具页对 OME-TIFF/转换器 BigTIFF 显示「已是可上传格式」并
  提供「上传到工作台」（点击前零 `/api` 请求）。格式目录文案修正：VMS/VMU
  属 Hamamatsu、SVSlide 属 Sakura，主页导航为「切片格式转换工具」（中英文
  同步）。
- **百度分享导入**：筛选层只放行 TIFF 类（`.tif/.tiff/.ome.tif/.ome.tiff`），
  其余候选不可选并给出原因码 `convert_in_browser_first`；下载后用同一核验
  函数复查字节，服务器转换分支不可达。
- **新错误码**：`convert_in_browser`（创建闸 422 与 worker 终态
  `fail_code`，带 `tools_url` 指向工具页）；`invalid_direct_class`（声明词
  表外或与扩展名错配，422）；`convert_in_browser_first`（百度原因码）。
  旧码 `conversion_moved_to_browser` 保留在转换 retry 的 410 路径，前端与
  新码映射同一文案。

## 3. 已验证的内容与命令

门禁记录（本轮报告撰写**未重跑**，结果引用门禁存档）：

- 全量门禁 `full-20261006-231450`（对应 HEAD `c064d21`，全部 rc=0）：
  `cargo test` 244 通过；vitest 855 通过；manifest 哈希 PASS；
  pytest 定向 791 通过 6 跳过；pytest 全量 3018 通过，仅 4 个既有基线失败
  （`test_admin_preview` 三项与 `test_ai_budget_wiring` 一项，与本分支无关，
  属允许范围）；C2 故障矩阵 54/54；no-whole-file 门 PASS（引擎与上传器仅
  有界切片）；C3/C4/R1 浏览器 E2E 全过；工作台 MRXS 文件夹场景 PASS；
  OOM 门 PASS（192 MiB 下 probe/convert 返回类型化
  `resource_profile_insufficient`）。
- 五个新格式的浏览器==原生 parity 与 192 MiB 内存门逐格式通过
  （`parity-scn/gtiff/ndpi/vms/raster`、`mem192-*` 全 PASS）。
- 阶段 1 门禁 `ui-20261005-182436`：vitest 780、C2 故障矩阵 43/43、两个新
  工作台场景 PASS（当时 pytest 定向的收尾等待输出格式问题已在全量门禁消除）。

真实样本验收（openslide-testdata 公开样本，CC0 或自由使用许可，均为明场；
报告撰写时用 venv 的 OpenSlide 终验通过）：

- SCN（Leica 公开样本，278 MB）：转换成功，三个组织 ROI 与源逐像素零误差，
  低倍层几何与 XML 声明一致。
- 通用 TIFF（Generic-TIFF 公开样本，204 MB、9 层全 JPEG 瓦片）：0.21 s 核心
  耗时、峰值 RSS 约 5 MB，全 tile 原样搬运零重编码。
- NDPI（Hamamatsu 公开样本，189 MB）：release 1 m27 s 转换成功，L0 三组织
  ROI 均值误差 0.040–0.055。
- VMS（Hamamatsu 公开样本，616 MB 压缩包）：preserve 1.76 GB / compact 694 MB
  转换成功，浏览器产物与原生 CLI 逐字节一致。
- 普通图片：用 CC0 SVS 源合成的大尺寸 JPEG 与小尺寸 BMP 验收，JPEG 带与不带
  restart marker 两条解码路径均与整帧解码逐字节一致。
- 样本环境清单已写入仓库外样本目录的 `ucf-samples.env`（含各样本 sha256 核对
  结果），总计约 1.46 GiB，未超 3 GB 预算。

本轮（报告撰写时）在本 worktree 执行的核对命令：`git status`/`git log`
（确认分支与 HEAD）、`python -c "from slide_format_registry import
public_catalog; ..."`（逐行打印上表数值）、`grep` 核对
`static/upload/slide-sniff.js` 的 zip 分类不带声明、`app.py` 的能力清单
生成、`tests/test_r1_conversion_gate.py` 的两张能力清单断言、
`tests/test_upload_direct_class.py` 的审查修复回归用例存在性，以及读取
全量门禁 `summary.txt` 与 `pytest-full.log` 确认 4 个基线失败名单。

## 4. 未覆盖的内容

- **无真实临床样本**：全部验收基于 openslide-testdata 公开样本与本机合成
  样本；真实扫描仪输出的非标准变体（不同 Aperio/Hamamatsu 固件、异常
  元数据）未验证。
- **操作系统与浏览器**：本轮仅 Linux 上验证；Windows/macOS 与
  Safari/Firefox（尤其文件夹拖放的 `webkitGetAsEntry` 遍历）未实测。
- **真实低内存设备**：192 MiB 门禁是受控 `MemoryMax` 环境，不代表低端
  真机的实际表现。
- **系统保存对话框**：产物落盘/下载的系统对话框交互路径未覆盖。
- **真实 COS 上传**：本分支新增格式未执行真实 COS 端到端上传（此前
  ux-formats 轮次对 SVS/MRXS 产物做过一次真实 COS 验收，本轮未重复）。
- **BIF 转换器**：因无可用公开样本未立项——公开 BIF 样本中可被
  libopenslide 4.0.1 打开的最小 RIGHT 方向样本为 2.53 GB（超下载预算），
  LEFT 方向样本按设计不被该版本支持；BIF 继续直接导入。

## 5. 需要用户决定的事项

1. **是否发布**：全量门禁已通过、工作区干净，可合入主干上线；若发布，
   建议随发布说明列出 §2 的用户可见变化与直传关闭清单。
2. **关闭哪些格式的直传**：§1 建议 ndpi/vms/scn 先关（需配套变体声明词表
   的后续小改动）、tif/tiff 第二步、raster 与无转换器格式维持开放；请确认
   顺序与范围。
3. **BIF 是否立项**：若需要 BIF 转换器，需先决定是否下载 2.53 GB 的 RIGHT
   方向公开样本用于验收，或等待更小的可用样本。
