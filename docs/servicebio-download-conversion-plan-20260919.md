# 塞维尔分享下载与多通道切片转换实现方案

日期：2026-09-19。状态：完成只读探索与方案设计，未实现代码、未转换全片、未部署。

修订：已纳入用户提供的同日review与测试记录。原始探索的线上观测、后续review报告的探针结果、本轮源码/哈希复核分别标注；本轮为文档修订，没有重跑所引用的pytest或像素探针。现有脚本只能作实现参考，阶段A–D均未因此视为完成。

## 1. 结论与本次项目定位

可以支持，但需要分别建设“Servicebio/Filez 分享下载”和“按实际内容识别、提取通道”的能力。塞维尔是交付方，不是一个统一文件格式；不能只在上传白名单里增加一个名称，或把所有文件改名为 `.svs`。

本次用户给出的四标荧光项目已找到对应历史数据与脚本：

- 分享页面当前可访问，目录元数据接口也成功返回；不能根据通知中“只保存15天”判定过期。接口此次返回 `expiration=-1`，这只是当前观测，不是未来可用性保证。
- 分享根目录包含一个切片子目录，子目录返回 `content_size=46`、`total_size=46`，包含 `Data0000.dat` 至 `Data0043.dat`、`Index.dat`、`Slidedat.ini`，没有 `.svs` 或 `.mrxs` 主文件。
- 按接口 `bytes` 相加，源文件总量 **1,796,983,062 bytes，约1.80 GB / 1.67 GiB**。不要使用文件夹自身的 `size=0` 作为下载量。
- 本次仅下载了 17,700 bytes 的 `Slidedat.ini` 验证单文件下载，没有下载全量 Data 文件。
- 线上 `Slidedat.ini` 与 homePC `/home/solarise/fourIFextract/raw/Slidedat.ini` 的 SHA-256 完全一致：`4814d0292a4df8ada99335ebbdaee5c5e9331877d6f3362ffaf4343580aaf947`。
- 元数据声明为荧光切片、当前格式版本2.3、328×732图像网格、20×物镜、存储位深8、JPEG压缩；同时相机原始位深字段为16。转换不能把交付的8位数据宣称为恢复了16位原始数据。

因此，homePC 的 `fourIFextract` 是本次项目的直接参考来源。元数据一致尚不能证明所有远程 Data 文件与本地副本逐字节一致；完整下载验收仍需成员大小、哈希和索引校验。

本地目录与远程交付必须区分：后续review报告，本地46个源成员的合计大小与线上清单一致，但 `raw/` 另有生成的 `slide.mrxs`。本轮复核确认其哈希与Slidedat.ini相同，是INI字节副本，不是空文件，也不是厂家主文件。源manifest只纳入交付成员；生成的入口、缓存、导出文件单列为派生产物，不计入46成员的源完整性判断。

## 2. 探索证据与可复用资产

以下路径均位于 SSH 别名 `homepc` 指向的服务器，不是当前工作站文件。

| 路径 | 实际能力 | 复用判断 |
| --- | --- | --- |
| `/home/solarise/.openclaw/workspace-muelsyse/skills/decode-svs/SKILL.md` | 记录11通道SVS、5通道MRXS提取方法 | 作为历史说明；本次未执行该skill工作流 |
| `/home/solarise/11channel/extract_channels.py` | 从4个RGB页按固定映射提取11通道；使用tifffile与imagecodecs | 适用于已经校准的样本，不是通用SVS解码器 |
| `/home/solarise/fourIFextract/extract_channels.py` | 解析Index.dat，按ROI读取两组荧光JPEG，产生5个灰度通道 | 本次项目优先参考；含缩放坐标修正 |
| `/home/solarise/fourIFextract/save_as_ometiff.py` | 输出CYX、5通道、Deflate BigTIFF | 是原型；全量分配数组，无金字塔，未写物理像素标定 |
| `/home/solarise/fourIFextract/raw/` | 本次对应的Slidedat.ini、Index.dat及Data文件 | 验证数据候选；先校验完整性 |
| `/home/solarise/fourIFextract/test_crop.ome.tif` | 现存约1.1MB测试产物 | 后续review已验证基础多通道读取，canonical缺口见2.2节 |
| `/home/solarise/tools/wsi_convert.py` | 使用OpenSlide把可读RGB切片转为金字塔OME-TIFF，含裁剪、白底、JPEG重编码 | 明场参考，不能代替荧光通道提取 |
| `/home/solarise/tools/README_转换说明.txt` | 记录历史SaiViewer BigTIFF、MRXS处理 | 是历史记录，兼容性及“无损”等表述需重新验证 |

### 2.1 副本差异和旧脚本风险

`fourIFextract/extract_channels.py` 的SHA-256为 `8031b8d433a4a8fbaba3939d510f7dbf7c453e2b554db8fbbabb6d8fc30a0de7`；skill目录下 `scripts/ref_5channle/extract_channels.py` 为 `45d40676d8bda20fa0f678f45ca5c39c5228e5fd0d019764b2723855027ff450`。两者不相同。

1. 项目版先按level-0的328列网格解码 `image_index`，再除以 `2^zoom`；skill副本没有这一步修正。生产实现应从元数据推导网格、层级和坐标，不能复制旧副本的算法。
2. 两版仍有样本专用目录、网格、层级编号和通道映射。Index指针需要增加边界、循环、长度、文件编号和解码尺寸校验。
3. 提取失败被 `except ...: pass` 吞掉，输出默认零值。生产必须区分未扫描区域与缺文件、损坏tile；后者不能变成“无荧光信号”。
4. 5通道OME输出原型全量分配 `5×H×W` uint8数组；历史区域约需33.5GiB，仅是像素缓冲，不含其他开销。必须改为分块读写设计。
5. 11通道脚本把多页全图读入内存，且通道名和重复槽位固定；不满足未知样本和大文件的生产要求。
6. 明场转换脚本按全宽×8192行读图，另保留二分之一分辨率RGB图；并非固定低内存。裁剪默认开启、透明区填白、JPEG质量92，这些行为不适合直接用于定量荧光。
7. `fourIFextract/README.md` 的“总计约60GB”与本次线上清单不符；其中JPEG颜色截断的成因解释属于历史推断，不能作为通用编码规律。

后续review的zoom-4探针：skill版382个key全部超出该探针的20×45网格，项目版有369个in-grid key，两者零重叠。该结果证明此输入/层级下旧副本不可用，不能扩大成“所有zoom>0输入必然整层为空”。项目版也仍须验证奇数尺寸的边缘tile、网格取整和扫描位置，不能直接作为生产正确性基准。

### 2.2 现有OME产物的实际合同

以下来自用户提供的后续review，不是本轮重新解码的结果。不能根据当前 `save_as_ometiff.py` 的参数反推历史产物属性。

| 产物 | review观测 | 验收判断 |
| --- | --- | --- |
| `test_crop.ome.tif` | CYX，5×1024×1024；非BigTIFF、非tiled、无金字塔/PhysicalSize | 可作小图读取fixture，不符合全片canonical合同 |
| `full_slide_preview_z4.ome.tif` | BigTIFF，5×6811×4123；无tiling、金字塔及MPP | 预览原型，不作为level-0分析数据 |
| `full_slide.ome.tif` | 约6.31GB，5×108978×65972，扫描区平面，无金字塔 | 压缩文件大小不代表解码内存；不能直接作为合格全片输出 |

review报告 `slide_io.open_slide(test_crop)` 返回TiffFileSlide，能暴露5通道名称，但没有MPP；普通 `read_region` 是第一通道灰度渲染。该兼容行为不等同于通道读取损坏，分析必须调用通道读取合同。reader默认显示色不能冒充源文件提供的颜色。

`ROIs.zip` 据review含6个8000×8000 JPEG，缺坐标。它可以作候选校准资产，但在确认每张图的通道、来源、level-0区域、尺度及导出参数前，不能作为几何或定量金标准。

## 3. 当前项目的接入缺口

以当前源码为准，不沿用旧计划中的阶段状态：

| 当前模块 | 已有能力 | 本次需要补充 |
| --- | --- | --- |
| `baidu_share_parser.py` | 粘贴文本提取百度分享；严格限定pan.baidu.com | 新增Servicebio解析器和provider路由，保留百度行为 |
| `baidu_adapter.py`、`baidu_import_http.py`、`baidu_import_store.py` | 枚举、选择、任务、租约、重试等百度导入链路 | 抽取可复用生命周期；Filez不能调用百度CLI |
| `baidu_ingest.py::ingest_staging` | 单文件校验入库，KFB/KFBF转后台转换 | 当前明确拒绝native-bundle，不能直接接收本次46文件 |
| `slide_format_registry.py` | SVS/TIFF等原生类型、MRXS bundle、KFB/KFBF转换类型 | 增加内容探测profile和bundle候选，不能把所有SVS改成需转换 |
| `slide_io.py::open_slide`、`TiffFileSlide` | OME优先读取，多通道及金字塔支持 | 对已识别的塞维尔页打包布局，禁止用“RGB能打开”替代通道完整性判断 |
| `conversion_store.py`、`conversion_worker.py` | 独立转换任务、产物及manifest发布 | worker目前按KFB/KFBF分派；需显式converter/profile选择和bundle源描述 |
| 上传入口、`slide_render.py`、`HistoPilot`通道合同 | 原生/转换上传、通道渲染与上下文 | canonical输出统一接入；细胞定量消费灰度通道及坐标，不使用截图作原始输入 |

当前能够打开一张RGB图，并不意味着所有荧光通道已正确暴露给分析程序。具体到本次链接，下载入口不支持Filez，文件又是缺少主入口的多文件包，两处都要补。

## 4. 下载实现方案

### 4.1 分享文本解析

用户直接粘贴整段交接通知。纯字符串解析阶段提取合法下载链接，识别 `pan.service-bio.com:10443/l/<slug>`，去掉中文标点、引号和Markdown包装；同段 `servicebio.cn/goodsdetail` 是浏览器教程页面，不作为切片下载候选。

用标准URL解析器验证scheme、精确host、端口、路径，拒绝伪装域名和userinfo。不能把`:10443`当作必然HTTPS：本次实际成功访问的是HTTP。HTTPS能力需独立验证，不能机械改写或关闭证书验证。多个有效分享应列出候选，重复Markdown链接去重。

建议返回 `provider=servicebio_filez`、规范分享地址及share标识。正文和完整带token地址不进入常规日志。

### 4.2 已实测的Filez访问协议

本次从实际页面及其JavaScript核对到：

1. `/l/<slug>` 重定向到 `/link/view/<delivery_id>`。
2. HTML提供 `linkInfo`，包含目录类型、保护状态及内部delivery标识；只解析JSON数据，不执行任意页面脚本。
3. 页面加载 `/js/link_share/LinkFolderPc.js`，调用 `/js/lenovodata/model/DeliveryManager.js` 中的metadata方法。
4. 本次实测 `GET /v2/delivery/metadata/<delivery_id>/?orderby=name&sort=asc&offset=0&limit=100` 返回根目录JSON；继续访问成员 `metadata_url` 得到46文件清单。
5. 成员提供 `is_dir`、`neid`、`path`、`bytes`、`modified`、`metadata_url`、`download_url` 等字段。ID按字符串处理，避免JavaScript大整数精度损失。
6. 单文件 `download_url` 已成功返回Slidedat.ini；目录 `download_url` 指向 `pkg_router`。本次没有调用打包下载，也没有验证大文件Range。

这是站点当前Web前端协议，不是已获得稳定性承诺的公开SDK。适配器需固定协议测试fixture并检测结构变化。分页参数的offset含义、大目录终止条件、提取码流程、URL有效期、Range/ETag及断链恢复仍需实测；不能由参数名字直接推断。

推荐接口职责：`resolve_share` → `list_children` → `build_candidates` → `download_member`。前端默认采用HTTP适配器；只有协议必须依赖交互时才单独评估浏览器辅助，不预设要常驻浏览器。

### 4.3 将文件组装成逻辑切片

枚举时按目录结构聚合：含Slidedat.ini、Index及其引用的Data成员的目录显示为“一张多文件切片”，不要显示为46个互不相干的unsupported文件。完整 `.mrxs + 同名目录` 同样聚合；普通SVS/TIFF保留单文件候选。

先枚举、计算字节数、确定成员manifest，再创建后台导入任务。推荐逐成员下载，保留层级，便于校验及续传；打包下载只作为明确支持的备用路径。源成员齐全并通过索引校验后才能转换，不边下载边对缺失tile补黑。

下载写入每任务隔离的staging，文件使用临时名；核对期望大小、计算SHA-256，完成后发布。断点只有在Range响应和源身份可确认时续接，否则重新下载。用户取消、配额超限、租约失效时停止并清理任务自有临时数据。

目录路径、ZIP成员禁止越界和符号链接逃逸，限制文件数、总解压大小及递归深度。每次访问metadata/download及重定向都检查域名、解析IP和端口；不能让服务端下发的URL任意访问内网。若合法下载跳转新增存储域名，经过验证后登记，不能开放任意URL。

分享失效、口令需求、禁止下载、上游不可达分别呈现。HTTP 200页面本身不能证明下载成功，要校验内容类型、字节数和文件头。通知保留时长仅作提示，过期状态以上游实际响应为准。

## 5. 格式识别与转换策略

### 5.1 内容探测合同

内容探测输出至少包括：container、variant/profile版本、dimensions、levels、axes、dtype、channel映射来源、MPP及来源、bundle成员完整性、可读性、可定量性、warnings。供应商和扩展名仅是线索。

| 内容类型 | 推荐处理 |
| --- | --- |
| 已支持且通道完整的标准TIFF/OME-TIFF/SVS | 原样入库，不重复有损转码 |
| 已校准的塞维尔多页RGB打包荧光TIFF/SVS | tifffile按页/样本槽读取，使用经过验证的profile转换为多通道OME-TIFF |
| 本次Slidedat.ini + Index.dat + Data文件 | 校验bundle；根据层级表、位置数据和通道profile提取，输出OME-TIFF |
| 完整明场MRXS | 复用原生bundle或按明确需求导出；保持全片坐标 |
| 未知页布局、加密/未知容器、无法确定通道映射 | 标记待确认或unsupported，提供实际探测结果，不能猜测通道名 |

SVS有其TIFF目录约定；将任意多页TIFF后缀改为SVS并不会产生兼容性。参见[OpenSlide Aperio格式说明](https://openslide.org/formats/aperio/)。

### 5.2 本次五通道提取

历史脚本给出的样本映射为：

| 存储层 | 提取结果 | 证据边界 |
| --- | --- | --- |
| FilterLevel_0 | R→GPNMB、G→KI67、B→DAPI | 历史样本经验映射，需对照原厂同ROI单通道导出 |
| FilterLevel_1 | B→CD3；`max(G-B,0)`→CD8 | CD3为直接槽位、marker解释待验证；CD8为派生估计，不能声称恢复原始强度 |

元数据滤光片名与生物marker是两类命名，名称不同本身不是映射错误的证据。review记录滤光片名为DAPI / SpGreen / SpOrange / CY5 / SpAqua；历史profile将其解释为DAPI / KI67 / GPNMB / CD8 / CD3。marker身份需要实验交接信息和参考导出支持，不能仅从滤光片字段推断。`STORING_CHANNEL_NUMBER` 也不能直接用作JPEG的R/G/B索引。

profile逐通道分别保存：`source_filter_name`、`marker_alias`、`source_layer`、`source_slot`、`extraction_kind`、公式、`mapping_evidence`、`validation_status`。顺序以profile和OME C索引显式绑定，不依赖目录排序。CD3虽直接取B，其身份和颜色解码仍待校准；CD8还需验证减法分离的误差。两者未经验证均不自动进入定量流程。

“四标”只指通知列出的四个marker，DAPI是否存在由文件和验证确定，不能直接令通道数等于4。本次历史脚本按5通道处理。

先获取厂家同一level-0 ROI的独立通道导出和通道对应说明，校准名称、强度、颜色解释、饱和、稀疏信号及几何对应。普通OpenSlide `read_region` 返回的RGB渲染结果不足以证明原始通道可分离。

若输入已发生颜色混合、饱和或JPEG有损压缩，后续Deflate输出只能避免再次损失，不能恢复丢失信息。manifest按通道记录 `direct_slot` / `derived`、公式、输入槽位、位深及验证状态。未经验证的派生通道不自动进入定量分析。

### 5.3 MRXS结构和几何

标准入口需要 `.mrxs` 与同名目录，目录包含Slidedat.ini；索引、位置表及数据共同决定图像。参见[OpenSlide MIRAX格式说明](https://openslide.org/formats/mirax/)。

远程交付没有主入口，但不能据此判断像素丢失。后续review用Slidedat.ini字节副本作为 `probe.mrxs`，配上 `probe/` 同名伴随目录，已让该环境的OpenSlide识别为MIRAX，报告10层和MPP 0.2738；单独主文件不能打开。这验证了该样本的入口包装可行性，不证明任意包装、其他版本或五通道分析兼容。生成入口只允许放在隔离staging并记录来源，不得改写源数据或称为“厂家补发的MRXS”。

review中一个tile的OpenSlide RGB读数与FilterLevel_0 JPEG一致，只证明该ROI的RGB浏览路径，不覆盖另一组FilterLevel或全片像素。入口包装可供浏览诊断，分析路径仍须独立完成通道和几何验证。

原型按规则网格直接拼tile。通用实现要读取真实层级表和扫描位置/拼接信息，处理重叠及偏移，校验多层和多通道共享坐标。仅按 `col×tile_size` 拼接不构成通用MRXS支持。

### 5.3.1 画布与坐标的实施决策

review识别出三套范围，不能混用其宽高或原点：

| 范围 | 原点/尺寸（level-0像素） | 用途 |
| --- | --- | --- |
| 原始规则网格 | 原点(0,0)，83968×187392 | 原始索引参考；5通道uint8约73.3GiB |
| 历史扫描区裁剪 | 原点(17249,63669)，65972×108978 | 现有full_slide产物；5通道约33.5GiB |
| OpenSlide bounds | 原点(17152,63488)，66304×109312 | reader报告的范围，不能替代前两者 |

建议统一以经过位置/拼接校准的源level-0坐标作为标注、ROI和分析结果的交换坐标。canonical默认保留该坐标域；如为减少空白采用裁剪存储，manifest必须保存 `source_dimensions`、`output_dimensions`、`crop_origin_level0`、`output_to_source_affine`、坐标定义版本及MPP来源。规则网格与reader空间未完成对照前，不得假设变换只有平移。

纯裁剪时 `x_source=x_output+x0`、`y_source=y_output+y0`；微米距离按X/Y各自MPP计算，不能只乘单一倍率。金字塔边缘尺寸按实际覆盖向上取整并验证最后一行/列，不直接使用右移截断。坐标合同和至少一组跨tile/跨层参考点验证是阶段B开工前置，物理坐标正确性是分析发布门槛。

### 5.4 canonical与外部导出

荧光canonical采用单文件、多通道、tiled金字塔OME-TIFF/BigTIFF：level-0每通道独立灰度平面，明确CYX或实际TCZYX轴；金字塔按OME SubIFD约定组织。保存channel names、显示颜色、PhysicalSizeX/Y及单位、原始dtype、T/Z、转换profile及源哈希。参见[OME-TIFF规范](https://ome-model.readthedocs.io/en/stable/ome-tiff/specification.html)。

level-0采用无损存储，不归一化、不伪彩合成、不缩小；金字塔仅用于浏览。预览的对比度调整不能写回分析像素。默认保持原始坐标；裁剪必须显式记录原点和仿射变换，MPP未知则保持未知，不套用另一张片子的标尺。

针对“之前那种TIFF分页储存的SVS”：先取得一份旧版可打开文件的匿名头部/页结构或测试样本，明确每页代表通道、扫描轮次还是分辨率。默认交付标准OME-TIFF，同时可提供每通道独立BigTIFF及通道清单；只有目标软件实际验证通过，才增加旧布局兼容导出。不能只改扩展名作兼容承诺。

## 6. 后台任务、权限和坐标合同

建议沿用现有导入及转换生命周期，增加provider无关的任务核心。百度适配继续使用现有接口，Servicebio拥有独立适配器；数据库迁移要保留旧任务查询和恢复，不直接修改所有百度记录含义。

逻辑阶段：解析 → 枚举 → 待选择 → 排队 → 下载成员 → 校验bundle → 探测 → 必要时转换 → 通道/几何验证 → 入库 → 关联项目。失败/取消和待通道确认应有独立状态。

- source manifest记录相对路径、成员大小/摘要、源版本及provider；下载URL/口令加密存储，页面与日志脱敏。
- profile指纹参与幂等和产物身份，同一个源更换通道解释不能复用旧转换结果。
- 重用owner校验、项目关联权限、租约、磁盘预留、配额、重试及删除逻辑。重试不得重复收费/计量或重复创建切片。
- 转换在独立进程分块执行，设置内存、CPU、超时、输出字节上限；成功验证后以任务独占方式发布TIFF与manifest，不覆盖同名他人资产。
- “可浏览”和“可定量分析”分别记录。验证不能仅调用一次RGB `read_region`；必须核对所有预期通道、level-0 ROI、尺寸和标尺。
- 下游分割返回level-0统一坐标，跨块对象需要去重。距离由真实MPP换算；裁剪/通道配准变换应传播到ROI、标注和距离计算。验证荧光通道并不等于已完成细胞分割算法接入。

建议稳定错误类别：`share_expired`、`share_password_required`、`share_download_forbidden`、`provider_protocol_changed`、`source_changed`、`bundle_incomplete`、`invalid_tile_index`、`tile_decode_failed`、`channel_mapping_required`、`channel_validation_failed`、`physical_scale_unknown`。其中缺标尺可以允许像素浏览，但不能默认给出微米距离。

## 7. 分阶段实施与验收

### 阶段A：下载协议与bundle入库基础

实现文本解析、Filez枚举和逐文件下载；本次46成员聚合为1张切片。验证分页、大整数ID、提取码/过期/禁止下载、Range支持与不支持、源变化、重试、取消、配额、跨域跳转及目录穿越。验收包括源成员清单完整、字节数一致、摘要稳定，不要求先有转换器。

阶段A终态是“源包就绪/待转换”，不是“可分析切片”。只有原生能力和通道验证通过的资产才能直接发布；本次bundle在阶段B/C通过前不得标为导入分析成功。允许先完成下载及结构完整性工作，不依赖厂家通道校准材料到齐。

### 阶段B：本次五通道profile

以homePC项目版脚本和匹配元数据为参考，先校准level-0 ROI，再实现有界内存的全片提取。补齐索引边界和位置变换、错误传播、MPP及manifest。至少比较组织中心、边缘、稀疏CD3/CD8区域、强阳性/饱和区域和跨tile边界。

验收：直接槽位与参考解码结果一致；派生通道与厂家参考的偏差、串扰及分割影响有预先约定门槛和结果记录；缺失/损坏tile必须失败或明确报告，不能假装背景。本次没有做这些像素验收，不能给出已通过的精度结论。

必须新增能区分旧行为与修复行为的测试：损坏JPEG、缺失被引用Data文件、索引循环/越界均不得产出成功全零通道；合法未扫描区允许补背景且保留有效区域信息。zoom-0/1/4以及奇数尺寸最后一行/列必须与已确认参考坐标对齐。全片输出记录峰值RSS和磁盘峰值，证明内存受块大小/缓存预算约束，禁止分配整个 `5×H×W` 或整页RGB数组。

### 阶段C：现有读取与分析链路

在PathTogether实际reader、通道切换及HistoPilot通道合同中验收C轴、名称、金字塔和坐标。OME输出用tifffile及目标外部软件交叉检查；选取已知像素间距的点核对X/Y非等距MPP、裁剪偏移与微米距离。验证断电/租约失效后恢复，发布和清理没有孤儿文件。

发布验证器检查BigTIFF、tile布局、各C/T/Z平面和SubIFD尺寸、全部预期通道、PhysicalSize及单位、显示色来源、profile/源哈希/坐标manifest；不允许以一次RGB读成功替代。小型单层TIFF可以保留一般读取支持，但不得被该全片转换任务误判为满足canonical合同。文件结构通过与通道定量验证通过分别记录；MPP未知或marker待确认时仅开放相应的受限浏览能力。

### 阶段D：扩展格式与兼容导出

单独验证11通道SVS及不同扫描仪版本，建立profile版本和合成fixture。对多页、ZYXS布局不得把页轴误当Z轴。旧SVS兼容输出必须用客户目标软件验收；未知变体保持待确认，不因vendor相同而放行。

## 8. 当前明确未验证的事项

1. 全量远程数据下载、所有成员哈希与homePC副本一致性。
2. 大文件断点续传、长期URL有效性、密码分享及大目录分页。
3. 厂家原始单通道参考、五通道映射的定量准确性和JPEG解码颜色解释。
4. 后续review已验证小crop在PathTogether的基础五通道读取，并确认缺MPP等元数据；全片随机读取、完整金字塔、修复后的标尺/坐标、客户软件兼容仍未验收。
5. 其他塞维尔扫描仪、文件布局和软件版本的通用支持。

本次仅修订此方案文档。现有业务代码、服务器脚本、数据库和生产服务未修改。

### 8.1 后续review测试记录与证据边界

以下为用户提供的同日测试汇总，本轮未重新执行。原报告未附完整命令、运行时版本、commit及原始日志，计数保留为历史证据，不作为未来实现的通过凭证：

| 测试/探针 | 报告结果 | 证明范围 |
| --- | --- | --- |
| 注册表和分享解析相关测试 | 25 passed | 现有合同回归，不证明Servicebio支持 |
| `test_b04_mixed_format_selectability`、`test_b06_native_kfb_kfbf_real_ingest_and_project`、`test_slide_io.py`、`test_conversion_task_api.py` | 96 passed | 原有导入/reader/转换合同的测试集结果 |
| Servicebio URL解析 | `unsupported_share` | 当前下载入口缺口 |
| INI/DAT单文件ingest；MRXS单文件ingest | `unsupported_format`；`baidu_bundle_unsupported` | 当前缺bundle聚合/导入 |
| 项目版512×512 ROI，原点(37376,87552) | 五通道非空，CD8稀疏 | 提取能运行，不证明marker或定量正确 |
| skill zoom-4、MRXS包装、现有OME读取 | 见2.1、2.2、5.3节 | 样本与环境限定的探针结果 |

本轮源码复核基线为PathTogether `6a9ce77`，确认bundle拒绝分支、KFB/KFBF分派及单次RGB校验仍在；homePC复核了项目版脚本指纹、dummy入口哈希和全量数组输出代码。该基线不能追溯替代前述测试运行的commit。

后续实施提交需保存：准确测试命令/选择器、代码commit、Python和reader/codec版本、输入manifest摘要、profile版本、ROI坐标/level、预期与实际结果、峰值资源和退出码。CI采用合成/匿名fixture；真实切片探针作为有明确样本条件的独立验收，缺样本应标记未执行。

## 9. 复核入口

站点协议来源：用户提供的Servicebio分享页面及该页面加载的 `LinkFolderPc.js`、`DeliveryManager.js`；实际调用其metadata接口并读取Slidedat.ini。为避免仓库保存访问凭据和客户名称，此文不嵌入完整分享地址、delivery ID、签名下载URL或原始目录JSON。

本地源码复核：`baidu_share_parser.py`、`baidu_import_store.py`、`baidu_ingest.py`、`slide_format_registry.py`、`slide_io.py`、`conversion_worker.py`。历史脚本路径及指纹见第2节。外部格式规范链接见第5节。
