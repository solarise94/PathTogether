# 先转换后上传 —— 交接报告（阶段 1 + 五个新输入转换器）

状态：**已完成，全量门禁通过，待发布决策**。分支 `upload-convert-first`；
全量门禁对应提交 `c064d21`，其后仅有本文档的更新提交；工作区干净（报告
修订时点）。本文汇总「先转换后上传」阶段 1（不新增转换器）与随后新增的
五个浏览器输入转换器（F4 SCN、F5 通用 TIFF/BigTIFF、F6 NDPI、F7 VMS、
F8 普通图片 BMP/JPEG）的最终状态、用户可见变化、验证情况与待决事项。

关联文档：规则与机制详见 `docs/slide-tools/upload-convert-first-phase1.md`；
五个转换器实测见 `docs/slide-tools/{scn,gtiff,ndpi,vms,raster}-adapter-report.md`；
SVS 转换器的样本验收见 `docs/slide-tools/f1-svs-adapter-report.md`。

提交脉络：阶段 1 四个提交（`fa2eb49` 后端、`4b01e74` 前端、`2d4b6f4` 浏览器
测试与文档、`9b7052b` 把工具页文件嗅探收敛到 `onFilePicked` 唯一汇合点，使
file input 与拖放两种入口走同一分类路径），基于 ux-formats `e043327`；阶段 1
审查三项发现（工作台 zip 误带声明 422、SVS 可被改名/zip 夹带绕过、嗅探内存
无界）已由 `2709f0c` 逐条修复并加回归测试；此后按 SVS 模式落地五个转换器
（F4 `382f41a`、F5 `9043489`、F6 `4090862`、F7 `180f36d`、F8 `0f35c60`），
最后两个提交 `81a15a9`/`c064d21` 修复全量门禁暴露的容器与测试环境问题。

## 1. 术语与机制速览

- **direct_class**：`POST /api/ingestions` 请求体的**可选 JSON 字段**（不是
  HTTP 头），值域 `ome-tiff | converter-bigtiff | legacy-direct |
  unconverted-variant:svs-jp2k`（`app.py` 创建接口与
  `upload_direct_class.py:49-61` 词表）。前端嗅探文件头后按类别在创建请求里
  携带：OME-TIFF 带 `ome-tiff`、转换器 BigTIFF 带 `converter-bigtiff`、
  JPEG2000 编码 SVS 带 `unconverted-variant:svs-jp2k`（该变体尚无浏览器
  转换器，凭声明继续直传）、其余直传格式带 `legacy-direct`（或不携带）。
  创建时校验词表与扩展名匹配并随任务落库（迁移 `0078`）。
- **嗅探**：共享模块 `static/upload/slide-sniff.js` 只读文件头（`Blob.slice`
  合计 ≤128 KB，绝不整文件读入），工作台与工具页共用同一入口
  `classifyFile`。
- **头级核验**：指按**文件头**（魔数 / IFD / 描述标签字节）复核声明是否与
  文件相符，与 HTTP 头无关。摄取 worker 在 `open_slide` 之前调用
  `upload_direct_class` 核验，不符 → 任务 FAILED，`fail_code=convert_in_browser`。
  实现集中在 `upload_direct_class.py`，全部读取有界（IFD 表 ≤1 MiB、描述
  ≤1 MiB；200 MiB 巨描述文件的嗅探峰值 RSS ≈14 MiB，回归测试
  `tests/test_upload_direct_class.py:194,247` 锁定）。
- **R1**：迁移收口计划（`docs/slide-tools/c6-migration-drain-plan.md` §3.1）
  的「一键转换并上传」阶段——服务器端转换下线，改为浏览器转换 + COS 直传
  （`docs/slide-tools/r1-convert-and-upload-report.md`）。KFB/KFBF 自 R1 起
  直传关闭。
- **C2 / C3 / C4**：浏览器切片工具的三个套件代号——C2 运行器（转换引擎，
  含 parity / 故障矩阵 / no-whole-file / smoke 门）、C3 独立工具页
  `/tools/slides`、C4 上传接入（分别见
  `docs/slide-tools/c{2,3,4}-*-report.md`）。
- **preserve / compact**：两种产物画质档位。preserve（保留画质，
  `preserve-source-v1`）按源参数重编码或原样搬运瓦片（如 q96 YCbCr 4:2:2）；
  compact（「更小文件（有损）」，`compact-jpeg-v1`）锁定 q80 4:2:0
  （`docs/slide-tools/u3-compact-encoding-report.md`）。parity 门对两档都做。
- **no-whole-file 门**：C2 的静态检查门（`test_no_whole_file.js`），核对
  转换引擎与上传器源码里只允许有界切片读取，禁止整文件读入内存。
- **创建闸**：`POST /api/ingestions` 创建时的直传关闭检查——直传关闭格式
  无合法声明 → 422 `convert_in_browser`（响应带 `tools_url=/tools/slides`）。

## 2. 各格式状态与直传建议

下表「浏览器转换」「直传当前」「模式」三列是本轮修订时用 venv Python 调
`slide_format_registry.public_catalog()` 逐行打印**核对所得**；「建议」
「理由」两列是**本报告作者的判断**，不是注册表内容。

| 格式 | 扩展名 | 浏览器转换 | 直传当前 | 建议 | 理由（作者判断，依据见括号） |
| --- | --- | --- | --- | --- | --- |
| Aperio SVS | .svs | 可用 | **关闭** | 维持关闭 | JPEG 编码 SVS 转换器有公开样本验收（f1 报告 §4：tile 载荷逐字节 100% 一致、bf-classic 经 OpenSlide 逐像素零误差）；JP2K 变体凭声明例外仍可直传；改名 `.tif` 与 zip 夹带均被内容级执行拒绝 |
| 3DHISTECH MRXS | .mrxs | 可用 | **关闭** | 维持关闭 | 必须整包（主文件 + 同名伴随目录），裸文件本就不可用；工作台文件夹交接已打通（C4 场景 `wb-drop-mrxs-folder` PASS），zip 藏 MRXS 解包前拒绝 |
| KFB / KFBF | .kfb / .kfbf | 可用 | **关闭** | 维持关闭 | R1 起服务端转换分支已下线，浏览器转换是唯一路径；百度侧 KFBF 批次按构造不可建（`tests/test_baidu_import_recovery.py`） |
| TIFF / BigTIFF | .tif / .tiff | 可用 | 开放 | 第二步再关 | 该行同时承载普通 TIFF（转换）与转换器导出的 BigTIFF（凭 `converter-bigtiff` 声明直传）。机制基础在位：`upload_direct_class.enforcement_failure` 已对 tif/tiff 名做内容级执行（`upload_direct_class.py:318-334`）；但「无声明的普通 TIFF 一律拒绝」**未实现、未验证**，属建议方向而非已验证结论 |
| Hamamatsu NDPI | .ndpi | 可用 | 开放 | **建议关闭** | 明场整层 JPEG 变体已真实样本验收（ndpi 报告 §3.1：198 MB 样本转换成功、浏览器产物与原生逐字节一致）；JP2K/渐进等变体需仿照 svs-jp2k 增加变体声明例外后再关行 |
| Hamamatsu VMS | .vms | 可用 | 开放 | **建议关闭** | 整包（入口 + 同目录 tile JPEG）转换器已真实样本验收（vms 报告 §3：preserve/compact 与原生逐字节一致）；VMU 无转换器、单独成行不受影响 |
| Leica SCN | .scn | 可用 | 开放 | **建议关闭** | JPEG 明场 SCN 已真实样本逐像素零误差验收（scn 报告：三组织 ROI 与源零误差）；荧光/非 JPEG 变体走暂时直传需保留例外 |
| 普通图片 | .bmp / .jpg / .jpeg | 可用 | 开放 | 维持开放 | **无使用量/摩擦数据的作者判断**：转换仅为重编码成 OME-TIFF（无物理标尺），直觉上对低价值图片关闭直传收益低、摩擦大；如需数据支撑应先看导入统计 |
| OME-TIFF | .ome.tif / .ome.tiff | 不可用（本身即目标格式） | 开放（direct-upload） | 维持开放 | 嗅探确认后的直接上传类别，是所有转换产物的落点 |
| Hamamatsu VMU | .vmu | 不可用 | 开放（暂时直传） | 维持开放 | 尚无浏览器转换器，关闭后无处可去 |
| Ventana BIF | .bif | 不可用 | 开放（暂时直传） | 维持开放 | 转换器本轮未立项、未验收（样本情况见 §6 第 3 条）；继续直接导入 |
| Sakura SVSlide | .svslide | 不可用 | 开放（暂时直传） | 维持开放 | 尚无浏览器转换器 |

关闭顺序建议：ndpi/vms/scn 一批（需配套变体声明词表的小改动，**该改动尚未
立项**）→ tif/tiff 第二步 → raster 不关。所有「关闭」沿用阶段 1 的目录行
机制：创建闸 422 + worker 头级核验 + 声明例外词表。

## 3. 用户可见的行为变化

- **工作台**：拖放扩大到整页（document 级，文件夹拖入经 `webkitGetAsEntry`
  遍历）；导入抽屉改为大拖放区 +「选择文件」+「选择文件夹（MRXS）」，完整
  格式目录收进折叠区，每行带三种徽标之一（直接上传 / 本机转换后上传 / 暂时
  直接导入）。选择 KFB/KFBF/JPEG SVS/MRXS 不再建服务器转换任务，而是弹窗
  交接 `/tools/slides` 本机转换（MRXS/VMS 携带整文件夹成员）。
- **转换可用且直传仍开放的格式的分流（NDPI/SCN/VMS/TIFF/普通图片）**：
  工作台拿到文件先按字节嗅探（`static/upload/slide-sniff.js:178-266`）——
  满足转换器判定的变体（整层 JPEG 明场 NDPI、JPEG 明场 SCN、通用瓦片
  JPEG TIFF、`.vms` 完整包等）→ 分类 `convert` → 弹窗交接本机转换后上传；
  不满足判定的变体（JP2K NDPI、荧光 SCN、条带/LZW TIFF 等）→ 分类
  `temporary` → 照旧直传；读头失败时按扩展名兜底（在 browser_convert 词表
  内 → 弹窗交接，`static/app.js:6638-6657`）。即服务端仍受理这些格式的
  直传，但前端把可转换变体主动引导到本机转换。
- **直传规则**：`.svs`（JPEG 编码）`.mrxs` `.kfb` `.kfbf` 不再受理直传；
  OME-TIFF 与转换器 BigTIFF 嗅探确认后带 `direct_class` 直接上传；其余格式
  照旧直传；zip 是运输容器、不携带声明（声明 `legacy-direct` 会被创建闸
  422——词表与扩展名匹配）。旧版缓存前端对 `.svs` 的上传：服务端行为由
  pytest 覆盖（`.svs` 无声明 → 422 `convert_in_browser`，
  `tests/test_ingestion_api.py:213-218`，对任何客户端生效），现行前端把该
  码映射为文案（`static/app.js:6988`、`static/i18n.js:804/2250`）；「真旧
  版前端缓存」的浏览器回归未做（见 §5）。
- **工具页**：对 OME-TIFF/转换器 BigTIFF 显示「已是可上传格式」并提供
  「上传到工作台」（点击前零 `/api` 请求）；格式目录文案修正：VMS/VMU 属
  Hamamatsu、SVSlide 属 Sakura，主页导航为「切片格式转换工具」（中英文同步）。
- **百度分享导入**：筛选层只放行 TIFF 类（`.tif/.tiff/.ome.tif/.ome.tiff`），
  其余候选不可选并给出原因码 `convert_in_browser_first`；下载后用同一核验
  函数复查字节，服务器转换分支不可达。
- **新错误码**：`convert_in_browser`（创建闸 422 与 worker 终态
  `fail_code`，带 `tools_url`）；`invalid_direct_class`（声明词表外或与
  扩展名错配，422）；`convert_in_browser_first`（百度原因码）。旧码
  `conversion_moved_to_browser` 保留在转换 retry 的 410 路径，前端两个码
  映射同一文案。

## 4. 已验证的内容（按证据归属分层）

**A. 本轮修订实际执行的检查**（命令与结果均为本轮实跑）：

- 基线 4 失败与分支无关的实证：`PYTHONPATH=.:tests .venv/bin/python -m
  pytest tests/test_admin_preview.py::test_subject_visibility_applies_during_preview
  tests/test_admin_preview.py::test_write_guard_blocks_all_unsafe_methods
  tests/test_admin_preview.py::test_cannot_preview_disabled_user
  tests/test_ai_budget_wiring.py::test_ui_budget_card_and_max_steps_sync_present -q`
  在本分支 HEAD 与 base `e043327`（临时 worktree）各跑一次，**两处同样
  4 failed**——这 4 项失败先于本分支存在；门禁流程对它们的豁免依据即此
  （门禁 summary 行原文「only the 4 known baseline failures allowed」）。
  失败内容为 admin 预览可见性/写保护 3 项与 AI 预算卡 UI 版本钉扎 1 项，
  均不触及本分支改动的上传/转换路径。
- 注册表目录行核对：`python -c "from slide_format_registry import
  public_catalog; ..."` 逐行打印 §2 表格前三列数值。
- 与主干的分叉核对：`git rev-list --count upload-convert-first..main` = 0、
  `...origin/main` = 0、`main..upload-convert-first` = 299——分支当前完整
  包含本地 `main`（`8955b7e`）与 `origin/main`（`7060d44`），可快进合入、
  无冲突面（以修订时点为准，主干此后前进则需重新核对）。
- 样本文件尺寸实测（`stat`）：NDPI 样本 198,030,965 B（= 198.0 MB 十进制
  = 188.9 MiB 二进制，**同一文件**，此前素材中两个数字只是单位口径差异）；
  通用 TIFF 样本 204,117,846 B；raster 样本 JPEG 9,619,181 B（6000×4000、
  质量 90）与 BMP 4,500,054 B（1500×1000 24 位）；OS-2.bif 2,717,833,684 B。
- 静态核对：`static/upload/slide-sniff.js:459-463`（zip 分类不携带声明）、
  `static/app.js` 能力清单生成、`tests/test_r1_conversion_gate.py:93-99`
  （browser_convert 与 direct_upload 两张清单断言）、审查修复回归用例
  （`tests/test_upload_direct_class.py:194,247,315-337`）存在性。

**B. 全量门禁存档（对应 `c064d21`；引用存档，本轮未重跑）**：门禁记录在
本 worktree 未跟踪目录 `.gate-tmp/gates/full-20261006-231450/`
（`summary.txt` 与各项日志）。全部条目 rc=0：`cargo test` 244 通过；vitest
855 通过；manifest 哈希 PASS；pytest 定向 791 通过 6 跳过；pytest 全量
3018 通过、4 failed（即 §4.A 实证的既有基线失败；**rc=0 是门禁脚本的汇总
退出码**，pytest 本身退出码非 0，门禁按允许清单豁免后判 PASS）；C2 故障
矩阵 54/54；no-whole-file PASS；C3/C4/R1 浏览器 E2E 全过；工作台 MRXS
文件夹场景 PASS；OOM 门 PASS（192 MiB 下 probe/convert 返回类型化
`resource_profile_insufficient`）。**五个新格式的 parity 与 192 MiB 内存门
（`parity-scn/gtiff/ndpi/vms/raster`、`mem192-*`）出自同一存档**，非本轮
重跑。阶段 1 门禁 `ui-20261005-182436`（vitest 780、C2 故障矩阵 43/43）
同属存档引用。

**C. 真实样本验收（引用各适配器报告与样本准备轮记录，本轮未复跑）**。
验收口径统一为两条：

1. **像素对源**：有逐字节搬运路径的输出（SVS、SCN、通用 TIFF preserve、
   NDPI/VMS 的 tile 重编码前载荷）要求 OpenSlide 对读逐像素零误差
   （max_abs_diff = 0）或 tile 载荷逐字节一致；只能整体重编码的比对（NDPI、
   VMS、compact 档、普通图片）用 **0–255 像素值尺度的平均绝对误差**，
   上限写在 pytest 断言里：NDPI L0 三组织 ROI 实测 0.040/0.055/0.044
   （max ≤5），断言上限 `< 6.0`（`tests/test_slide_transform_ndpi.py:318-322`，
   依据：量级 = 一次高画质 JPEG 生成损失）；VMS 实测 0.033–0.045，同上限
   6.0（`tests/test_slide_transform_vms.py:361-377`）。
2. **产物一致性**：同一输入的浏览器产物与原生 CLI 产物**逐字节一致**
   （C2 parity，preserve 与 compact 双档），这是所有格式的硬门。

各格式的实测数字与出处：SVS——`f1-svs-adapter-report.md` §4.1–4.2（公开
CC0 样本 CMU-1/CMU-2/CMU-1-Small-Region：tile 载荷 100% 逐字节一致、
bf-classic ROI 零误差；JP2K 样本按设计拒绝）；SCN——`scn-adapter-report.md`
（278 MB 真实样本，三 ROI 逐像素零误差，wall 0.36 s、峰值 RSS ≈4.9 MiB）；
通用 TIFF——`gtiff-adapter-report.md`（204 MB、9 层全 JPEG 瓦片，核心
0.21 s、峰值 RSS 5.1 MB，全 tile 零重编码）；NDPI——`ndpi-adapter-report.md`
§3.1（198,030,965 B 样本，release 构建的原生 CLI 耗时 1 m27 s，preserve
产物 501,836,506 B，compact 产物 201,041,264 B）；VMS——`vms-adapter-report.md`
§3（preserve 产物 1,761,618,512 B，compact 产物 693,552,459 B、耗时 276 s）；
普通图片——`raster-adapter-report.md`（合成样本尺寸见 §4.A 的 stat 实测；
JPEG 带/不带 restart marker 两条解码路径与整帧解码逐字节一致）。
样本能否被 OpenSlide 打开的终验出自样本准备轮记录（venv OpenSlide binding
1.4.6 / libopenslide 4.0.1，scn/gtiff/ndpi/vms 全部通过），本轮未复跑。

样本复核入口：样本为 openslide-testdata 公开样本（CC0 或自由使用许可），
清单与 sha256 记录在与 PathTogether 仓库同级的项目样本目录
`.testdata/openslide/ucf-samples.env`；样本下载轮总下载量 1.46 GiB，未超
该轮 3 GB 的**总量**预算（BIF 的 OS-2.bif 2.53 GiB 当时因 1.46+2.53≈4.0
超过总量预算而未下载；现状已变化，见 §6 第 3 条）。

## 5. 未覆盖的内容

- **无真实临床样本**：全部验收基于 openslide-testdata 公开样本与本机合成
  样本；真实扫描仪输出的非标准变体（不同固件、异常元数据）未验证。
- **操作系统与浏览器**：本轮仅 Linux 验证；Windows/macOS 与
  Safari/Firefox（尤其文件夹拖放的 `webkitGetAsEntry` 遍历——MRXS/VMS
  文件夹交接依赖它）未实测。
- **真实低内存设备**：192 MiB 门禁是受控 `MemoryMax` 环境，不代表低端
  真机。
- **系统保存对话框**：产物落盘/下载的系统对话框交互路径未覆盖。
- **真实 COS 上传**：五个新格式的转换产物未执行真实 COS 端到端上传（此前
  ux-formats 轮对 SVS/MRXS 产物做过一次，本轮未重复）。若关闭
  ndpi/vms/scn 直传，浏览器转换产物上传将成为这些格式唯一的入库路径，
  该缺口见 §6 第 4 条。
- **旧版前端的浏览器回归**：`.svs` 422 的服务端行为与现行前端文案映射有
  测试覆盖（§3），但「缓存了阶段 1 之前 JS 的真实浏览器」场景未做端到端
  回归。
- **BIF 转换器**：本轮未立项、未验收（样本现状见 §6 第 3 条）。

## 6. 需要用户决定的事项

1. **是否发布、是否先补跨平台验证**：全量门禁通过；分支当前完整包含本地
   `main` 与 `origin/main`（可快进合入，§4.A），建议合入前对主干终点重跑
   快速门禁以防主干此后前进。若发布，是否要求先补 Windows/macOS（及
   Safari/Firefox）抽样验证——文件夹交接是 MRXS/VMS 的核心路径而它依赖
   仅在 Chromium 系实测过的 API，请决策。
2. **关闭哪些格式的直传、何时关**：§2 建议 ndpi/vms/scn 先关、tif/tiff
   第二步、raster 与无转换器格式维持开放。配套的变体声明词表改动**尚未
   立项**；关行之前，JP2K NDPI、荧光 SCN、条带 TIFF 等变体的直传保持
   敞开，窗口时长由关行排期决定。tif/tiff 第二步的判定信号（例如发布后
   观察期内无直传相关故障）需 owner 定义。
3. **BIF 是否立项**：现状更新——RIGHT 方向公开样本 OS-2.bif
   （2,717,833,684 B，sha256 已校验）已在样本目录就位，`ucf-samples.env`
   已有 `BIF_SAMPLE` 行（该下载发生在本报告所述各轮之后，已超出当时
   3 GB 总量预算的约束）；BIF 转换器仍未立项。样本前提现已具备，是否立项
   由 owner 决定。
4. **真实 COS 补验**：是否在发布前对 ndpi/vms/scn 的浏览器转换产物补一次
   真实 COS 端到端上传验收，或明确接受 §5 所列缺口上线。
