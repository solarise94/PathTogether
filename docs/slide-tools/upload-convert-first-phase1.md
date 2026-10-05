# 先转换后上传 —— 阶段 1（不新增转换器）

状态：已实现（分支 `upload-convert-first`）。
范围：把「选对格式 → 才能上传」的产品规则落地为**前端分流 + 服务端声明
核验**；不新增任何扫描仪转换器。KFB/KFBF/JPEG 编码 SVS/MRXS 一律先在本机
浏览器（切片格式转换工具 `/tools/slides`）转换为 OME-TIFF/BigTIFF 再上传。

关联文档：`docs/slide-tools/c6-migration-drain-plan.md`（R1 服务端转换
关闭）、`docs/cos-only-upload-agent-plan-20260928.md`（COS 统一上传）。

## 1. 规则

### 1.1 类别（前端嗅探 + 注册表目录行）

前端共享模块 `static/upload/slide-sniff.js`（工作台与工具页同一份）只用
`Blob.slice` 读文件头（合计 ≤128 KB，绝不读整个文件），把文件判为：

| 类别 | 判定 | 处理 |
| --- | --- | --- |
| `ome-tiff` | TIFF 且 ImageDescription 含 OME-XML（命名只是提示——`.ome.tif` 命名但字节非 OME 按 temporary 处理） | 直接上传，创建时声明 `direct_class=ome-tiff` |
| `converter-bigtiff` | 经典 BigTIFF 且描述 JSON 带转换器来源标记（`source_format ∈ {kfb_bf_v1, kfb_kfbio_jpeg, aperio-svs-jpeg, mirax-bundle}`，与 `kfb/converter.py`、`slide-transform-core` `convert_svs.rs`/`convert_bf.rs`/`convert_mirax.rs` 的描述写法同词表） | 直接上传，`direct_class=converter-bigtiff` |
| `convert`（需要转换） | KFB、KFBF、JPEG 编码 Aperio SVS（IFD0 压缩=7）、MRXS（`.mrxs`/`.dat` 成员） | 弹窗交接 `/tools/slides` 本机转换后上传 |
| `temporary`（暂时直传） | 尚无浏览器转换器的格式/变体：JPEG2000 编码 SVS（压缩 33003/33005）、NDPI、VMS、VMU、SCN、BIF、SVSlide、BMP/JPEG、普通 TIFF、zip | 照常直传（无声明或 `legacy-direct`；SVS-JP2K 凭 `unconverted-variant:svs-jp2k` 声明） |
| 不支持 | 未登记扩展名 | 明确报错，不建任务 |

服务端注册表（`slide_format_registry.public_catalog()`）每行对应新增两个字段：
`browser_convert`（`available`/`unavailable`）与 `direct_import`
（`open`/`closed`），`import_mode` 由此派生为三值：`direct-upload`
（OME-TIFF 行）、`convert`（svs/mrxs/kfb/kfbf）、`direct-temporary`（其余）。
导入抽屉徽标三种：「直接上传」「本机转换后上传」「暂时直接导入」。
名称修正：VMS/VMU 属 Hamamatsu，SVSlide 属 Sakura（中英文案均已改）。

### 1.2 本阶段关闭的直传

- **JPEG 编码的 `.svs`**（目录行级关闭）：无声明 → 422；JPEG2000 变体凭
  `unconverted-variant:svs-jp2k` 声明例外放行（worker 核验第 0 层压缩）。
- **zip 中含 MRXS 包**：worker 解包前扫中央目录，命中即终态失败。
- **KFB/KFBF**：维持 R1 起的关闭（conversion 分支拒绝）。
- 裸 `.mrxs`：维持不可直传（需整包，走本机文件夹转换）。

## 2. 错误码

| 码 | 场景 | 形态 |
| --- | --- | --- |
| `convert_in_browser` | 创建闸：直传关闭格式无合法声明 / conversion 形态；worker：声明与文件头不符（任务 `fail_code`）；zip 内藏 MRXS | 422/终态，带 `tools_url=/tools/slides` |
| `conversion_moved_to_browser` | 旧码，保留在 `POST /api/conversions/<id>/retry` 的 410 与前端兼容映射（与 `convert_in_browser` 同文案） | 410，带 `tools_url` |
| `invalid_direct_class` | `direct_class` 词表外 / 与扩展名错配 | 422 |
| `convert_in_browser_first` | 百度筛选层（不可选原因码）与下载后字节复查 | candidate 行 `reason_code` / 条目失败码 |

## 3. direct_class 声明与核验

- `POST /api/ingestions` 可选字段 `direct_class`：
  `ome-tiff | converter-bigtiff | legacy-direct | unconverted-variant:svs-jp2k`。
  词表与扩展名匹配在创建时校验（大小合同之后裁定，均不建行/不占预约）。
  声明随任务落库（迁移 `0078_ingestion_direct_class.sql`，存量行 NULL）。
- 摄取 worker（`cos_ingest_worker`）在 `open_slide` 试开**之前**按
  `upload_direct_class.declaration_matches` 核验（只读魔数/首 IFD，绝不读
  整个文件）：`ome-tiff` 必须 tifffile `is_ome`；`converter-bigtiff` 描述
  JSON 必须带转换器来源标记；`unconverted-variant:svs-jp2k` 第 0 层压缩必须
  为 33003/33005。不符 → 任务 FAILED，`fail_code=convert_in_browser`。
  `NULL`/`legacy-direct` 不做头级核验（字节合法性仍由 `open_slide` 终审）。
- 核验实现集中在 `upload_direct_class.py`（摄取 worker 与百度路径共用）。
- **已入库切片的读取路径不受任何影响**：本阶段不触碰 `slide_io`/阅读器/
  manifest 语义；`direct_class` 只是上传闸门的声明证据。

## 4. 能力下发

`bootstrap.capabilities.cos_upload` 与 `GET /api/tools/slides/upload-capability`
的 `cos_upload` 载荷新增两张静态清单（available=false 时也随形下发）：

- `direct_upload.formats`：当前受理直传的扩展名（tif/tiff/ome.tif/ome.tiff/
  ndpi/vms/vmu/scn/bif/svslide/bmp/jpg/jpeg）；
- `browser_convert.formats` + `url`：有浏览器转换器的扩展名
  （svs/kfb/kfbf/mrxs）与工具页地址。

既有 `formats`（受理词表，含 zip）语义不变。

## 5. 端上行为

- **工作台**：导入抽屉本地页改为大拖放区 + 「选择文件」+「选择文件夹
  （MRXS）」；完整格式目录收进折叠 `<details>`。拖放扩大到整页
  （document 级 dragenter/over/leave/drop，复用 `#drop-overlay` 计数；
  文件夹拖入经 `webkitGetAsEntry` 遍历——含 `.mrxs`/`.dat` 成员按完整包
  交接，普通目录逐文件分流）。拿到文件按 §1.1 分流：直传类立即上传
  （创建带 `direct_class`）；转换类走既有弹窗交接（消息扩展 `bundle`
  成员数组 + `folderName`，工具页 `prepareBundleSource` 接收）；暂无转换
  器的照旧直传；不支持的明确报错。
- **工具页（/tools/slides）**：文件选择 accept 增加
  `.ome.tif/.ome.tiff/.tif/.tiff`；拖入/选择 OME-TIFF/转换器 BigTIFF 不进
  转换，显示「已是可上传格式」并提供「上传到工作台」（直传控制器
  `tools-slides-direct-upload.js`：登录/账号绑定/上限/格式受理检查与产物
  上传同一语义，数据源换成用户的 File）；工作台交接来的此类文件直接上传
  并关联交接目标项目。用户点击前仍然零 `/api` 请求。
- **百度分享**：筛选层只放行 TIFF 类（`.tif/.tiff/.ome.tif/.ome.tiff`），
  其余候选不可选，原因码 `convert_in_browser_first`（「请先在本机转换为
  OME-TIFF 后再上传」）；下载后用同一核验函数复查（OME-TIFF/转换器
  BigTIFF/普通 TIFF 放行）；`_ingest_convert` 分支从 `ingest_staging`
  不可达（拒绝并给出同一原因码）。

## 6. 兼容性

- 旧前端（缓存 JS）：`.svs` 创建会被 422 `convert_in_browser` 拒绝并有
  明确文案；其余行为不变。
- 旧任务/店内直建任务（`direct_class IS NULL`）：worker 不做头级核验，
  行为与升级前一致。
- `conversion_moved_to_browser` 旧码保留在 conversion retry 410 路径；
  前端两个码映射同一文案。
- 百度侧在途批次：已存在的 convert 条目在收口时按 §3 复核拒绝
  （`convert_in_browser_first`），不产生新转换任务。

## 7. 测试

- pytest：`tests/test_upload_direct_class.py`（嗅探/声明正负例、worker
  集成负例：声明 ome 但非 OME、声明 svs-jp2k 但 JPEG SVS、zip 内藏 MRXS）；
  `tests/test_ingestion_api.py`（`convert_in_browser` 拒绝、词表/扩展名
  匹配、声明落库）；`tests/test_baidu_imports.py`/`test_baidu_ingest.py`
  （TIFF-only 筛选、KFB 不再转换）；`tests/test_slide_format_registry.py`/
  `test_conversion_task_api.py`/`test_raster_wiring.py`（目录行新字段与
  import_mode 三值）。
- vitest：`tests/js/slide-sniff.test.ts`（分类矩阵，手工构造 TIFF 头）、
  `tests/js/workbench-direct-class.test.ts`（工作台分流）。
- 浏览器：`tests/browser/slide_tools_c4/run_workbench.js` 新增
  `wb-drop-ome`/`wb-drop-svs-convert`/`wb-drop-mrxs-folder`；
  `run_e2e.js` 新增 `p-direct-ome-upload`（工具页 OME-TIFF 直接上传）。
