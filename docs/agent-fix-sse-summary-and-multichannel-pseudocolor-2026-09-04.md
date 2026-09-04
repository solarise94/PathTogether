# PathTogether / HistoPilot：SSE 恢复、长总结布局与多通道伪彩显示——单 Agent 实施任务书

> 日期：2026-09-04  
> 状态：**排查完成；本文档仅是实施规格，业务代码尚未修改**  
> 执行方式：一个 agent 按批次顺序完成；每批先补失败测试，再实现，再跑本批门禁  
> 涉及仓库：`PathTogether`（主仓）与 `HistoPilot`（插件仓）；当前两个 checkout 均为 `wip/ser8-dev`，执行时仍须重新记录分支/HEAD/上游  
> 本轮边界：不提交、不推送、不部署；执行 agent 未获得明确授权前也不得做这些动作

## 1. 目标与已经确认的结论

这不是三个互不相关的小改动。最终产品必须同时满足：

1. AI 的 SSE 网络连接短暂中断后，前端能恢复同一个运行，而不是先显示“network error”再让用户触发一个必然得到 409 的新运行。
2. AI 完成长总结不能从一个被 flex 压缩的状态行里溢出，并覆盖下一条消息。
3. OME-TIFF 多通道图像不再只取 `C=0`；查看器可以选择通道、使用默认色卡伪彩合成，并明确告诉用户每个颜色来自 OME 元数据还是系统默认。
4. 人类查看器、缩略图、导出区域、分享页和 AI 快照使用同一份规范化 `render_context`；界面明确显示“AI 已同步当前通道配色”或“AI 仍使用上一版配色”。
5. “通道着色”只是显示映射，不得伪装成生物学结论，也不得与矩形/箭头/自由描图等正式病理标注混为一谈。

### 当前根因

- `PathTogether/slide_io.py:TiffFileSlide._build_index()` 对除 `Y/X/S` 外的轴一律取 0，因此 `CYX`、`TCZYX` 的 `C` 轴被静默固定为第一个通道。
- `TiffFileSlide._to_rgb_u8()` 对非 `uint8/uint16` 数据按每次读取数组自己的 min/max 拉伸。瓦片之间使用不同强度范围，会出现亮度跳变和接缝；它也无法表达多通道显示状态。
- 主站、Demo、分享服务分别构造 DZI、瓦片和缩略图 URL；当前主站瓦片内存缓存键为 `(name, level, x, y)`，没有文件 generation，更没有渲染指纹。多种配色若复用该键会串色。
- HistoPilot 的 `SlideDescriptor`、`RegionRequest`、`image_ref`、`last_snapshot_view` 和 checkpoint hash 目前均不知道通道渲染上下文。只改查看器会造成“用户看 A，AI 实际看 B”。
- `HistoPilot/integrations/pathtogether/ui/main.js` 将 SSE `reader.read()` 抛错统一当作运行失败并执行 `finishAiRun()`；但 `PathTogether/app.py:_proxy_sse()` 明确允许浏览器断开后 sidecar 运行继续。因此前端清掉本地 running 状态后再次发送，会以新 `request_id` 遇到 sidecar 的“会话正在运行中”409。
- `agent_finished` 把完整 `p.summary` 放进 `.ai-status.finished`。移动端 `.ai-trace` 是纵向 flex，后面的全局 `.ai-status { min-height: 0 }` 又允许该项收缩；长文字的可视溢出覆盖下一条气泡。

## 2. 术语与不可混淆的语义

| 术语 | 含义 | 不代表什么 |
|---|---|---|
| 通道（channel） | OME 像素模型中的 `C` 维逻辑通道 | 不是 RGB 的 `S` 样本轴 |
| 通道着色（channel coloring） | 将一个标量通道映射为显示 RGB 的伪彩规则 | 不是染色真实性声明，也不是病理标注 |
| 通道颜色说明卡 | 通道开关、色块、名称、颜色来源、强度范围的 UI | 不是 annotation/ROI 列表 |
| 正式标注（annotation） | 已写入 PathTogether 标注库的矩形、箭头、描图等对象 | 不因换色而新增、移动或删除 |
| `render_context` | 一次显示/派生图使用的通道、颜色、强度窗、平面和版本 | 不是授权令牌 |
| `render_context_fingerprint` | 规范化渲染上下文的 SHA-256 | 不是切片资产 revision |
| `render_token` | 服务端签名的、可在瓦片 URL 中携带的渲染上下文 | 不授予切片访问权限，仍须原有鉴权 |

所有用户可见文案使用“通道着色/显示映射/默认伪彩”，不要单独写“颜色标注”，避免与正式标注歧义。

## 3. 范围与非目标

### 3.1 首版必须支持

- 普通 OpenSlide RGB 切片：行为、颜色与性能保持兼容。
- 单文件 OME-TIFF：`CYX`、`CZYX`、`TCYX`、`TCZYX` 及其金字塔 level；逻辑 `C` 通道可以独立读取和合成。
- OME 原生 RGB(A)：仅当 TIFF photometric 明确为 RGB 且 `S=3/4` 时按原生颜色处理，不把 `S` 错当多个荧光通道。
- `uint8`、`uint16` 和有限值 float 通道。
- 主站、Demo、分享查看器、缩略图、瓦片、crop、`/api/slide/<name>/region`、插件 v1 region 与 AI 快照的一致着色。
- 桌面与 325 px 宽移动端。

### 3.2 首版明确不做

- 不做 Z-stack/MIP、时间序列播放或任意 `T/Z` 选择。若 `SizeT>1` 或 `SizeZ>1`，首版固定 `T=0,Z=0`，在颜色说明卡顶部持续提示，API 也返回结构化 warning。
- 不支持跨多个物理文件的 multi-file OME-TIFF；检测到外部 UUID/缺失 plane 时清晰报错，禁止悄悄显示不完整数据。
- 不做光谱解混、背景扣除、定量共定位分析或“真实荧光颜色”推断。
- 不根据 `Name/Fluor` 猜 DAPI/FITC/Cy5 颜色。显式 OME `Channel@Color` 可信；缺失时只使用标明来源的默认色卡。
- 不新增服务器端“用户预设”数据库表。用户调整保存在浏览器本地；刷新时重新向服务端规范化。分享链接默认打开服务端默认方案，不承诺携带分享发起者的临时方案。
- 不把合成 PNG/JPEG 当作原始科学数据导出。crop 下载必须提示“当前显示合成图”；原始通道导出另立需求。

## 4. 用户可见的目标行为

### 4.1 普通 RGB 切片

- 不显示占空间的通道面板；工具栏最多显示灰色小标识“原始 RGB”。
- 旧 DZI 路径仍可访问；旋转、镜像、ROI、标注、分享和 AI 行为不回归。

### 4.2 多通道切片

打开切片后，在查看器工具栏提供“通道”入口。面板必须显示：

- 总通道数与当前启用数，如“已显示 3/6 个通道”。
- 每个通道：开关、色块、`Name`（缺失时“通道 1”）、原始索引、颜色来源。
- 颜色来源只允许：
  - “OME 元数据颜色”：XML 中确实存在 `Channel@Color`；
  - “默认伪彩色卡”：属性缺失或非法，使用确定性色卡；
  - “用户调整”：本次浏览器会话中被用户改过。
- 元数据完整性摘要，如“4/6 有名称；2/6 有 OME 颜色；其余使用默认伪彩”。
- 若 `T/Z>1`，显示“当前仅显示 T=0、Z=0；时间/层面切换尚未支持”。
- 默认最多启用前 4 个有效通道；所有通道都可列出；一次最多启用 8 个。达到上限时阻止第 9 个并给出可理解提示。
- 控件不能只靠颜色传达状态；必须同时有文本、勾选态、焦点样式和可读的 `aria-label`。

### 4.3 AI 同步提示

AI 面板固定显示一行简短状态：

- `AI 已同步：C0 青 / C1 洋红 / C2 黄`：当前空闲会话的最后一次 run 使用的 fingerprint 与查看器一致。
- `AI 未同步当前配色；仍在看上一版通道组合`：用户改了通道、颜色或强度窗，但还未发起下一次 AI 动作。
- `本次 AI 运行已锁定配色；显示调整将在下一条消息同步`：运行中改变查看器显示。

下一次 `run/continue/ask/branch` 必须携带当前规范化 `render_context`。PathTogether 校验后再交给 HistoPilot，HistoPilot 记录一次 `render_context_changed`（仅在 fingerprint 改变时）并在新一轮使用它。不得在一个正在生成的模型请求中途替换图片语义。

### 4.4 crop 与截图

- crop、缩略图、AI snapshot 与屏幕瓦片默认使用同一个 `render_token`。
- crop 下载文件名加入 render fingerprint 前 8 位，并在 UI 确认文案写“导出当前伪彩合成图”。
- AI 的图片旁或轨迹中显示精简 legend；模型上下文也收到文字 legend，明确“这些颜色是显示映射，不等同于组织染色事实”。

## 5. 默认颜色与渲染算法（不得自行改口径）

### 5.1 OME RGBA 解析

OME `Channel@Color` 是有符号 32 位 RGBA。解析规则：

```text
u32 = signed_value & 0xffffffff
r = (u32 >> 24) & 0xff
g = (u32 >> 16) & 0xff
b = (u32 >> 8)  & 0xff
a = u32 & 0xff
```

- 必须区分“XML 属性不存在”和“属性显式等于 -1”。后者是显式白色 `#FFFFFFFF`，来源仍是 OME 元数据。
- 非整数、越界或 XML 无法对应到当前 series 的颜色视为缺失，并产生 warning。
- alpha 参与默认权重；`a=0` 的通道默认关闭并提示。用户重新选色后使用 alpha 255。

### 5.2 缺色时的确定性色卡

按逻辑通道索引循环使用以下顺序；不要随机，也不要用 Python/JS 进程哈希：

```text
0 #00FFFF 青       4 #FF5C5C 红
1 #FF00FF 洋红     5 #4D7CFE 蓝
2 #FFD166 黄       6 #FF8C42 橙
3 #00E676 绿       7 #B388FF 紫
```

色卡只保证显示区分度，不声明荧光团真实颜色。UI 必须显示“默认伪彩色卡”。

### 5.3 全局强度窗

禁止逐瓦片 min/max。定义 `global-percentile-v1`：

1. 对每个通道从可用的最低分辨率金字塔层做确定性网格采样，最多 262,144 个有限像素；不得加载 level-0 全图。
2. `black = P0.1`，`white = P99.9`，`gamma = 1.0`。
3. 若 `white <= black`：整数回退到 dtype 有效范围；float 回退到全局有限 min/max；仍无范围则标记 `empty_or_constant`，默认关闭并提示。
4. NaN/Inf 不参与统计；输出时按 0 处理并计指标。
5. 统计结果按 `(asset_generation, series_index, t, z, algorithm_version)` 缓存；同一 generation 只能由一个 worker/thread 计算，其余等待或复用，不能产生不同结果。

单通道归一化：

```text
n_i = clamp((raw_i - black_i) / (white_i - black_i), 0, 1) ** (1 / gamma_i)
```

多通道使用线性加色并裁剪，顺序无关：

```text
rgb = clamp(sum(n_i * alpha_i * color_i_rgb), 0, 1)
```

最终统一量化为 sRGB `uint8`。算法版本写入 `render_context.version = "multichannel-additive-v1"`；任何公式或统计口径变化都必须升版本，不能悄改同一 fingerprint 的像素结果。

## 6. 规范化数据契约

### 6.1 PathTogether info 响应（向后兼容新增字段）

`GET /api/slide/<name>/info`、Demo/share 对应 info，以及插件 v1 slide descriptor 增加可选字段：

```json
{
  "image_mode": "multichannel",
  "axes": "TCZYX",
  "plane": {"t": 0, "z": 0, "size_t": 1, "size_z": 1, "policy": "first-plane-v1"},
  "channels": [
    {
      "index": 0,
      "id": "Channel:0:0",
      "name": "DNA",
      "color": "#00FFFF",
      "alpha": 1.0,
      "color_source": "ome",
      "default_active": true,
      "dtype": "uint16",
      "intensity": {"black": 23.0, "white": 14870.0, "gamma": 1.0, "source": "global-percentile-v1"}
    }
  ],
  "default_render_context": {
    "version": "multichannel-additive-v1",
    "asset_revision": "opaque-existing-revision",
    "plane": {"t": 0, "z": 0},
    "active_channels": [{"index": 0, "color": "#00FFFF", "alpha": 1.0, "black": 23.0, "white": 14870.0, "gamma": 1.0}],
    "fingerprint": "64-char-lowercase-sha256"
  },
  "default_render_token": "opaque-signed-token",
  "deepzoom": {"width": 100000, "height": 80000, "tile_size": 512, "overlap": 1, "min_level": 0, "max_level": 17},
  "warnings": []
}
```

- RGB 切片返回 `image_mode: "native_rgb"`，`channels: []`，默认 context 版本为 `native-rgb-v1`。
- 所有新增字段为 additive/optional；旧客户端仍能用原 DZI。
- `asset_revision` 与 `render_context_fingerprint` 是两个独立值，禁止互换。
- `channels` 可以超过 8；限制的是 `active_channels`。

### 6.2 渲染上下文规范化端点

三个访问面都提供不落库的规范化端点：

```text
POST /api/slide/<name>/render-context
POST /api/demo/slides/<name>/render-context
POST /s/<share-token>/api/slide/<name>/render-context
```

请求只提交用户选择：`active_channels[]`、`plane`。服务端：

1. 先执行原有 `can_view_slide` / Demo capability / share token 授权；
2. 验证索引唯一且存在、启用数 1..8、RGB 为 6 位十六进制、alpha 0..1、black/white 有限且 white>black、gamma 0.1..5；
3. 绑定当前 asset revision，排序字段，按固定小数序列化 canonical JSON；
4. 计算 SHA-256 fingerprint；
5. 返回 canonical `render_context` 与确定性 HMAC `render_token`。

`render_token` 使用与应用 secret 派生的独立用途 key；payload 含 canonical context 和 slide asset revision。它不含用户身份、不授予权限、不得跳过每个资源端点原有鉴权。多 worker 与分享服务必须可以无共享内存地验证。secret 轮换后旧 token 失效时，前端刷新 info 并重建一次，不无限重试。

### 6.3 资源端点

以下端点接受 `render=<render_token>`；缺省时使用该切片当前默认 context：

- tile、thumbnail、crop、`/api/slide/<name>/region`；
- Demo/share 的对应 tile、thumbnail、crop；
- `/api/plugin/v1/slides/<slide>/regions` 与过渡期 `/internal/ai/region`，wire body 使用 `render_context`，不要把长 token 转交给 HistoPilot 持久化。

若 token 中 revision 与当前文件不符，返回稳定错误 `409 slide_revision_conflict`。前端只允许刷新 info 并重建一次；连续变化沿用现有 `SlideFileChanged -> 503` 语义。

### 6.4 HistoPilot TypeScript 契约

在 `HistoPilot/src/platform/contract.ts` 新增可选 camelCase 类型：

```ts
interface RenderChannel {
  index: number;
  color: string;
  alpha: number;
  black: number;
  white: number;
  gamma: number;
}

interface RenderContext {
  version: "native-rgb-v1" | "multichannel-additive-v1" | "legacy-first-plane-v0";
  assetRevision: string;
  plane: { t: number; z: number };
  activeChannels: RenderChannel[];
  fingerprint: string;
}
```

并做以下 additive 改动：

- `SlideDescriptor.renderContext?`、`channels?`、`warnings?`；
- `RegionRequest.renderContext?`；
- `RegionResult.renderContextFingerprint?`；
- PathTogether HTTP adapter 负责 snake_case/camelCase 翻译，core 不得 import Flask 私有结构。

滚动部署兼容：旧 PathTogether 不返回这些字段时按 `native-rgb-v1`；旧 session/image_ref 没有 context 时使用 `legacy-first-plane-v0` 重放，保持旧的首平面语义，禁止拿升级后的默认多通道合成冒充历史快照。

## 7. PathTogether 后端实现要求

### 7.1 重构边界

新增共享模块（建议 `slide_render.py`），由 `app.py` 与 `share_server.py` 共用：

- channel manifest/OME RGBA 解析；
- render context canonicalization、fingerprint、签名/验证；
- 全局统计缓存；
- 多通道区域读取与合成；
- DeepZoom `RenderedSlideView` 适配。

禁止在主站、Demo、分享服务复制三份颜色算法。`slide_io.py` 只负责可靠像素/元数据访问；路由鉴权仍归各 Flask app。

### 7.2 `TiffFileSlide` 必改项

- series 选择不能再按包含 `C/T/Z` 的全 shape 乘积最大化；按 `Y*X` 主空间面积、有效金字塔层和确定性 tie-break 选择，避免小空间高通道辅助 series 抢主图。
- 暴露结构化 `axes/shape/series_index/channel_manifest/plane_sizes`。
- 新增只读取指定 `channel_indices`、`t=0,z=0` 的区域方法；zarr 索引必须保留 `Y/X` slice 和所选 `C`，不得加载所有 plane 后再裁切。
- `S` 只在 photometric RGB + 3/4 samples 时作为原生 RGB(A)；逻辑 `C` 单独处理。
- 每个 pyramid level 都以该 level 自己的 axes/shape 构建索引；必须验证 level 间 `C/T/Z` 尺寸一致，异常时 fail clearly。
- 原有 `read_region()` 保持 OpenSlide duck-type 兼容；多通道通过 `RenderedSlideView.read_region()` 应用 context。
- 完全越界、部分越界与 alpha padding 的现有语义保持不变。

### 7.3 DeepZoom 与缓存

- RGB 老客户端可继续 `.dzi`；新版多通道前端使用 OpenSeadragon inline custom TileSource，`getTileUrl(level,x,y)` 带 `render_token`。不要依赖 DZI XML 是否保留 query string。
- tile cache key 至少为：

```text
(safe_name, file_generation, render_context_fingerprint, level, x, y, format, quality)
```

- 主站当前缺少 generation，必须与分享服务一样改为 `slide_cache.read_stable()`/generation-aware；否则同名替换和长期 immutable 浏览器缓存会返回旧图。
- tile URL 必须包含 asset revision 和 render fingerprint（可封装在 token 中），使浏览器缓存真正内容寻址。
- `RenderedSlideView` 只包装当前借出的 `osr`；不要把绑定某一池句柄的 wrapper 跨 borrow 或跨线程缓存。
- 统计缓存与 tile 缓存有独立容量、命中/淘汰指标；关闭或替换切片时两者都可按 generation 淘汰。

### 7.4 资源限制与错误

- channel manifest 与 token 验证必须发生在解码前。
- 继续执行 crop/plugin region 的像素预算与并发闸；真实成本估算乘以启用通道数，并设合理上限，不能把 8 通道当单通道计费。
- 返回稳定机器码：`invalid_render_context`、`render_channel_out_of_range`、`render_channel_limit`、`unsupported_multifile_ome`、`unsupported_plane_selection`、`slide_revision_conflict`。
- 日志只记 slide 的既有安全标识、fingerprint 前缀、通道数、耗时和错误码；不记录 token 全文或图像内容。
- 必须加指标：manifest/统计耗时、每通道解码耗时、合成耗时、tile cache hit/miss、context 校验失败、NaN/Inf 数。

## 8. 前端实现要求

### 8.1 共用组件

在 `viewer-core.js`（或独立、被三种页面共同加载的 `channel-controls.js`）实现：

- `normalizeChannelInfo(info)`；
- `createDeepZoomTileSource(info, adapter, renderToken)`；
- 通道说明卡渲染、键盘操作、颜色 picker；
- localStorage 读取/写入与 asset revision 失效；
- 当前 context 与 AI context fingerprint 比较。

`app.js`、`demo.js`、`share.js` 只负责各自 adapter 和权限，不再各写一套通道 UI。

### 8.2 Adapter 扩展

`app-mode.js` 以及分享页 adapter 增加：

- `normalizeRenderContext(id, body)`；
- `tileUrl(id, level, x, y, renderToken)`；
- `thumbnailUrl(id, renderToken)`；
- `regionUrl/regionBody` 所需 context。

颜色变化的更新顺序必须是：取消旧 tile 请求/关闭旧 world item → 服务端规范化 → 更新 `state.renderContext` → 更换缩略图 → 打开新 TileSource → open 后恢复旧 viewport center/zoom/rotation/flip → 更新 AI 同步徽章。旧请求晚到不得覆盖新 context。

### 8.3 本地偏好

- key 包含用户作用域、slide 安全名和 asset revision；不得只有文件名。
- 只存用户选择，不存服务端签名 token。
- 通道索引/数量、版本或 revision 变化时丢弃旧选择并回默认。
- localStorage 解析失败只回默认并给一次非阻塞提示；不得阻止打开切片。

## 9. AI 渲染上下文闭环

### 9.1 运行入口

`run/continue/ask/branch` 的浏览器请求带 canonical `render_context`。PathTogether 代理必须：

1. 根据当前切片 revision 再校验；
2. 不信任浏览器传入的 fingerprint，服务端重算；
3. 将 camelCase context 注入 HistoPilot run config；
4. 审计 session、request_id、asset revision、render fingerprint，不能记录 token。

### 9.2 HistoPilot 持久化与缓存

以下位置必须携带或纳入 `render_context_fingerprint`：

- session 当前 render context；
- `image_ref`；
- `snapshot_captured` payload 与 `last_snapshot_view`；
- derivative spec/cache key；
- checkpoint/stable-prefix hash 的视觉身份部分；
- `transform-context` 重新物化 region 的请求。

同一 slide/bbox/level 但不同 context 必须得到不同 derivative key；重放旧 `image_ref` 必须使用它自己的 context，不能使用查看器当前 context。

模型收到的每张新 context 图片前都要有精简 legend，例如：

```text
Display mapping (not a biological annotation): C0 DNA=#00FFFF (OME metadata),
C1 Channel 2=#FF00FF (default pseudocolor); T=0, Z=0; render fp=ab12cd34.
```

正式 annotation 的几何/label schema 本批不迁移。AI annotation 仍通过 session/snapshot 追溯其显示上下文；不要把 render fingerprint 塞进 label/note，也不要新建伪标注。

### 9.3 UI 事件

- 新增 `render_context_changed` 领域事件，只在 run 接受新 fingerprint 后发出。
- UI 切换显示本身不是 HistoPilot 事件，不能伪造 AI 已看见。
- `snapshot_captured` 继续是“AI 当前视角”的唯一权威来源；增加 context 字段不改变现有 viewport/observation/annotation 语义。

## 10. Bug A：SSE 断线后的恢复

### 10.1 正确状态机

至少区分：

```text
idle -> submitting -> streaming -> reconnecting -> terminal
                    \-> submit_unknown -> reconciling -> streaming/terminal
```

- POST 尚未拿到响应头/没有 session id 时失败：使用**同一个请求 body 和同一个 `request_id`**重试；绝不能生成新 id。sidecar 已支持 request-id 幂等。
- 已得到 session id 后 `reader.read()` 失败：保持 `aiRunning=true`，显示“连接中断，正在恢复”，用最后确认的 `seq` 请求：

```text
GET /api/ai/session/<id>/stream?after_seq=<last_seq>
Last-Event-ID: <last_seq>
```

- backoff：0.5s、1s、2s、4s、8s，叠加不超过 20% jitter；最多 5 次。网络恢复/页面可见时可立即再试一次，但同一时刻只能有一个 reader。
- 达到上限后调用 session 状态和 `/session/by-request/<request_id>` 对账：
  - running：继续显示“后台仍在运行”，提供“重新连接”与“取消”，不允许盲发新 run；
  - paused/finished/error：按真实 terminal 恢复 UI；
  - 404 且提交结果未知：显示可重试错误，重试仍复用原 request_id。
- 明确的用户取消、切片切换、组件销毁使用 AbortController；这些 `AbortError` 不重连。
- `finishAiRun()` 只能在确认 terminal/取消后调用，并清理 typing bubble、controller、reader、timer；错误分支不能留下截图中的灰色三点气泡。

### 10.2 必改文件

- `HistoPilot/integrations/pathtogether/ui/sse.js`
- `HistoPilot/integrations/pathtogether/ui/main.js`
- `HistoPilot/integrations/pathtogether/ui/api.js`
- 必要时 `PathTogether/app.py` 的 stream/reconciliation proxy，但不要改变“client disconnect 不终止 backend run”的正确服务端语义。

### 10.3 回归测试

- reader 在两个事件后 reject：重连 URL 使用最后 seq；事件不重复；最终 finished。
- POST 在响应头前断线：两次请求 request_id 相同，sidecar 只产生一个 session/计费动作。
- 重连期间点击发送：被 UI 状态机阻止，不出现第二个 request_id/409。
- Abort/切片切换：零重连。
- 五次失败后 session 仍 running：UI 仍是后台运行态，可手工重连/取消。
- 每条失败路径都不存在残留 typing bubble、timer 或双 reader。

## 11. Bug B：长总结覆盖下一条消息

### 11.1 结构修复

- `agent_finished` 的完整 `p.summary` 渲染为普通 assistant message bubble，可换行、自然增高。
- `.ai-status.finished` 只显示短状态，如“本次读片已完成”；不要再次复制全文。
- `.ai-trace > .ai-status { flex-shrink: 0; min-height: auto; }` 作为防御。
- 当前后置的通用 `.ai-status` 规则实际用于 Demo 时，将 selector 收窄为 `.demo-ai-panel > .ai-status` 或明确 id；不要让它覆盖主轨迹组件。
- header “进行中/已完成”只反映当前 active session/run；历史完成总结不强行改变一个新 run 的 header。

### 11.2 真浏览器验收

用真实生产 CSS 和至少 1,200 字中文 summary，在 325×700 与桌面 viewport 断言：

```text
finishedBubble.boundingBox().bottom <= nextBubble.boundingBox().top
finishedBubble.scrollHeight <= finishedBubble.clientHeight
```

另存测试虚构文字截图；验证滚动到底、切换会话、刷新恢复 transcript 后仍不重叠。

## 12. 单 Agent 分批执行顺序

任何批次失败都停在该批，不得把未跑/skip/红灯描述为完成。

### Batch 0：基线与失败测试

1. 记录两个仓库 `git status --short --branch`、HEAD、上游；保留用户已有改动。
2. 运行两仓当前核心门禁，记录真实通过数、skip、耗时。
3. 添加合成 OME fixtures 和前述 SSE/layout 失败测试；确认它们在旧实现上按预期失败，而非 fixture 自己坏。
4. 不接触真实患者图像；测试数据必须确定性生成。

### Batch 1：修复 SSE 与长总结

1. 先完成第 10、11 节。
2. 跑 HistoPilot UI/SSE 单测与 Chromium layout E2E。
3. 运行 HistoPilot 全量和 build；本批全绿再进入多通道。

### Batch 2：OME manifest、像素读取、全局统计

1. 改 `slide_io.py` 的 axes/series/channel 语义。
2. 新建共享 render 模块与纯函数单测。
3. 覆盖 CYX/TCZYX、S-RGB、dtype、NaN/Inf、非法 XML、多 series、multi-file 拒绝。
4. 验证不会读取未选 plane 或 level-0 全图。

### Batch 3：路由、token、缓存与派生图

1. info + render-context API。
2. 主站/Demo/share 的 tile、thumbnail、crop、region 统一接入。
3. generation + render fingerprint cache key；revision 冲突和多 worker token 验证。
4. 像素预算按通道数计费；跑并发与同名替换测试。

### Batch 4：三种查看器通道 UI

1. 共用 TileSource/说明卡组件。
2. 主站接入，再接 Demo/share；不要复制算法。
3. viewport 保持、快速连点竞争、localStorage 失效、键盘/读屏、移动端。
4. RGB 旧切片视觉基线不变。

### Batch 5：HistoPilot 跨仓闭环

1. 扩展 Platform Contract 和两个 adapter。
2. run 绑定、session/image_ref/snapshot/checkpoint/derivative 持久化。
3. legend 与 AI 同步/未同步 UI。
4. 同一 bbox 两种 context 的缓存隔离、旧 session `legacy-first-plane-v0` 回放。
5. 更新并真实运行 cross-repo contract；不得通过条件 skip 假绿。

### Batch 6：全量、发布前与在线验收

1. 两仓全量测试、覆盖率、PG canary、Chromium、bundle、跨仓契约。
2. 若获授权，按仓分别提交；记录各自 SHA，不把工作区当 Git 仓库。
3. push/CI/deploy 需单独授权和证据。
4. 部署后用无患者合成多通道切片做在线验收；验证服务器静态资源 hash 与预期 commit。

## 13. 测试矩阵

### 13.1 合成图像矩阵

| 场景 | 必须断言 |
|---|---|
| RGB SVS/TIFF | 像素与旧基线一致，无通道面板回归 |
| 单通道 `YX/CYX` | 灰度可着色，颜色来源正确 |
| 2/4/8 通道 `CYX` | 开关、颜色、合成公式、缓存隔离 |
| 12+ 通道 | 全部列出，默认仅 4，一次最多 8 |
| `TCZYX`, T/Z=1 | 等价 CYX |
| `TCZYX`, T/Z>1 | 固定 0 且 API/UI 明示 warning |
| OME explicit RGBA | signed int 正确解码，含 `-1` 与 alpha=0 |
| Color 缺失/非法 | 使用确定性色卡并标“默认伪彩” |
| Name 缺失 | 显示“通道 N”，不猜荧光团 |
| uint8/uint16/float | 使用全局窗；相邻瓦片无 min/max 接缝 |
| float NaN/Inf/常量 | 不崩溃、有 warning、像素确定 |
| 多 series | 按主空间图选择，不被高 C 小图抢占 |
| multi-file OME | 稳定拒绝，不显示不完整数据 |
| 同名文件替换 | revision/token/cache 全部换代，不返回旧瓦片 |

### 13.2 一致性矩阵

对同一 bbox 和 context：

- 屏幕 tile 拼接、crop、普通 region、plugin binary/JSON region、AI snapshot 的中心像素容许 JPEG 误差但颜色方向一致；
- thumbnail 与全图最低层同 context；
- 主站、Demo、share 输出相同 fingerprint；
- 更换任一颜色/black/white/gamma/通道开关后 fingerprint 必变；字段顺序变化但语义相同则 fingerprint 不变；
- 切片 revision 改变后旧 token 必须 409，不能命中旧 cache。

### 13.3 并发/失败矩阵

- 20 个并发请求同一 context：统计只计算一次，无句柄越界或串色。
- 两种 context 并发同一 tile：结果不同且各自稳定。
- 计算统计时文件替换：丢弃旧 generation，最多按现有策略重试一次，连续变化 503。
- render token 篡改、越界索引、NaN、超 8 通道：解码前 4xx。
- SSE 在 POST 前、POST 后、事件中间、terminal 前断开；每种都保持 exactly-one logical run。

## 14. 实际门禁命令

从工作区 `/Users/solarise/ZCodeProject/histopilot-suite` 执行；不要对工作区根运行 git。

### PathTogether

```bash
cd PathTogether
python -m pytest tests/test_slide_io.py tests/test_slide_render.py -q
RUN_PG_TESTS=1 python -m pytest tests -q --cov=. --cov-report=term --cov-report=xml --cov-fail-under=72
python -m pytest tests/test_pg_backend_canary.py -q -rA
npm run test:js
npx playwright install chromium
npm run test:e2e:admin
```

要求：PG 全量使用仓库既有测试自举且 `RUN_PG_TESTS=1` 口径与 CI 一致；canary 输出不得含 skipped/no tests ran。若新增 viewer E2E 脚本，纳入现有 Playwright config/CI，而不是只在本机手跑。

### HistoPilot

```bash
cd HistoPilot
npm run build
npm test
npm run test:coverage
npm run bundle:pathtogether
PATHTOGETHER_REPO=/Users/solarise/ZCodeProject/histopilot-suite/PathTogether npm run test:contract
```

跨仓契约输出不得出现非零 skipped。若本机依赖不能真实启动，报告环境阻塞，不得把跳过写成通过。

### 静态与仓库检查

```bash
git -C PathTogether diff --check
git -C HistoPilot diff --check
git -C PathTogether status --short --branch
git -C HistoPilot status --short --branch
```

## 15. 兼容、迁移与回滚

### 15.1 数据迁移

- 首版无 PostgreSQL schema 迁移、无 annotation schema 迁移。
- session JSON/DB 中新增字段必须 optional；旧记录懒兼容，不批量重写。
- localStorage 以版本化 key 存新数据；删除该 key 可完全回退。

### 15.2 部署顺序

使用 feature flag `PATHTOGETHER_MULTICHANNEL_ENABLED`：

1. 先部署可读 optional 字段的 HistoPilot；
2. 再部署 PathTogether 后端，flag 关闭，仅暴露/测试 server capability；
3. 部署新版前端 bundle；
4. 合成图 smoke test 后开启 flag；
5. Demo/share 分别验收。

旧客户端无 context 时服务端走旧兼容；新客户端连接旧后端时看不到 `image_mode=multichannel`，必须保持原 viewer，不发送新字段。

### 15.3 回滚

- 首选关 flag：隐藏通道 UI，资源端点回到 native/legacy path；不删除缓存或用户数据。
- HistoPilot 保留 optional 字段读取，旧 PathTogether 仍可工作。
- 若 render 模块异常，回滚应用版本；新 session 字段因 optional 不阻止旧版本读取。
- 不用 `git reset --hard`、不清空上传目录、session、annotation、队列或数据库。

## 16. 完成定义与证据层级

“完成”必须分别报告，不能合并成一句“全绿”：

1. **文档完成**：本文和 API/schema 说明更新。
2. **代码完成**：两仓 diff 列表、未覆盖的非目标。
3. **本地测试完成**：逐条命令、通过/失败/skip 数、耗时、测试环境。
4. **提交完成**：仅在授权后，分别给 PathTogether/HistoPilot commit SHA。
5. **推送完成**：分别给远端分支与远端 SHA。
6. **CI 完成**：列出四类 PathTogether 门禁、HistoPilot coverage 与 cross-repo contract 的真实链接/结果。
7. **部署完成**：环境、部署 SHA、静态资源 hash。
8. **在线验收完成**：真实浏览器网络断线恢复、长总结移动端、多通道主站/Demo/share/AI 一致性截图或测试 artifact。

只有 1—8 中获得授权且实际完成的层级可以写“已完成”。本任务书本身只建立第 1 层的一部分，不是实现证明。

## 17. Agent 停止条件与禁止事项

遇到以下任一情况先停并报告证据：

- 两仓存在与目标文件重叠的用户未提交修改，无法安全绕开；
- 合成 OME fixture 与 tifffile 2024.5.22 的真实 axes/level 不一致；
- 为支持 multi-file OME、Z/T 选择或用户服务器预设必须扩展首版范围；
- 必须新增数据库列或改变 annotation 几何/权限语义；
- PG/Chromium/cross-repo contract 被 skip 或无法真实运行；
- 部署、secret 轮换、push、删除缓存/数据等需要新授权。

禁止：

- 只在前端叠几层 CSS filter，却让 crop/AI 使用另一图；
- 对每块瓦片单独自动对比度；
- 把通道名猜成颜色并不告知用户；
- 把 render token 当访问授权；
- 把全通道数组读入内存后再切 tile；
- 用新 request_id 恢复同一次未知状态的 AI 提交；
- 把完整 summary 再塞回状态行；
- 用 mock-only/skip 测试宣称支持真实 OME、PostgreSQL 或 Chromium；
- 修改正式标注语义来承载显示颜色。

## 18. 外部规范依据

- OME-TIFF 的 plane/`DimensionOrder`/`FirstC` 语义：<https://ome-model.readthedocs.io/en/latest/ome-tiff/specification.html>
- OME 2016-06 `Channel@Color`：RGBA、有符号 32 位、`-1 = #FFFFFFFF`：<https://www.openmicroscopy.org/Schemas/Documentation/Generated/OME-2016-06/ome_xsd.html>
- tifffile 将 OME-TIFF 表达为带 axes 的多维 series，且当前仓库固定版本为 2024.5.22：<https://iridescent.ink/tifffile/tifffile.html>
- OpenSeadragon inline custom TileSource 可由 `width/height/getTileUrl` 构造：<https://openseadragon.github.io/examples/tilesource-custom/>

这些链接是实现约束的来源，不替代仓库内对固定依赖版本的真实 fixture 测试。
