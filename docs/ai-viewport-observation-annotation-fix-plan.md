# AI 视角、临时观察区与正式标注修复方案

- 日期：2026-08-24
- 状态：**已实施（2026-08-24）**。批次 A/B/C 与批次 D 的代码侧核查全部完成；实施记录见文末 §13
- 范围：PathTogether Demo Viewer、PathTogether HostBridge、HistoPilot PathTogether 插件及 HistoPilot 工具/会话契约
- 涉及仓库：`PathTogether`、`HistoPilot`
- 产品决定：只保留“当前 AI 视角、临时观察、正式标注”三类语义；**本轮不增加扫描覆盖、覆盖热图或覆盖率展示**。

---

## 1. 背景与问题表现

当前 Demo 中，用户能看到多个长期保留的绿色矩形，例如“全片概览”“某处低倍致密”“某处高倍细胞”。这些框表面上像 AI 的逐级取景过程，也像 AI 已经确认并标出的病灶，但实际来源是 `mark_observation` 产生的临时 observation。

目前至少有三层混淆：

1. AI 的实际取景范围没有被稳定、单独地呈现。
2. 每张快照的读片小结与真正的局部证据区都通过 `mark_observation` 表达，并统一画成绿色框。
3. 正式写入标注库的 AI 标注与临时 observation 虽然数据路径不同，但面向用户的用词和空间表达不够清楚。

这不是单纯的标签遮挡或颜色问题，而是对象语义、事件契约、几何约束和前端状态模型没有对齐。

## 2. 已确认的当前实现问题

### 2.1 Demo 完全没有跟随当前 HistoPilot 的真实导航事件

当前 HistoPilot **不发送独立的 `goto` SSE 事件**。`goto` 工具执行后发送的是 `tool_started`，其 payload 为 `{tool:"goto", x, y, level, magnification, reason, requested_level}`；PathTogether 的 `_proxy_sse` 只做字节透传，不会把它改写成 `type="goto"`。

Demo 的 `handleEvent("goto")` 分支因此在当前契约下不可达；该分支读取的 `p.zoom` 也不存在。与此同时，Demo 的 `tool_started` 分支又显式跳过 `tool="goto"`。所以当前实际症状不是“只平移不变倍”，而是 **Viewer 完全不跟随 AI 的 goto 移动**。

真正权威的取景范围在 `snapshot_captured.bboxLevel0` 中；它是平台裁剪后实际回喂给模型的 level-0 bbox。当前 Demo 收到该事件后只追加“抓取视野”轨迹，不导航 Viewer，也不设置独立的当前视角框。

正式 HistoPilot 插件已经在 `snapshot_captured` 后调用 `navigateWithOverlay`，按实际 bbox 导航并外扩边距。Demo 与正式插件因此出现行为分叉。

### 2.2 临时观察被当成历史视角轨迹展示

Demo 把所有 `observation` 事件追加到 `state.observations`，并长期以同一种绿色样式绘制。旧观察不会随着新快照切换而隐藏，也没有“当前快照/选中观察”的可见性规则。

Prompt 又建议每次快照都先调用 `mark_observation`，导致模型容易为以下内容都创建 observation：

- 全片概览小结；
- 中间倍率的导航确认；
- 真正值得关注的局部证据。

最终，历史快照的小结框、不同倍率的局部框和当前正在看的区域一起叠在 Viewer 上。

### 2.3 observation 几何缺少服务端约束

当前 `mark_observation` 的 `x/y/w/h` 全部可选。缺失或非法值会被转换为 0；工具只检查当前是否存在 pending snapshot，没有检查：

- 数值是否有限；
- `w/h` 是否大于 0；
- bbox 是否位于切片边界内；
- bbox 是否位于当前 pending snapshot 内；
- observation 是否只是整张快照的重复框。

因此 observation 与“模型刚才实际看见的图像”之间没有强制证据关系。

### 2.4 observation 与 snapshot 的关联没有完整传到 UI

Session 中的 observation 已保存 `snapshot_id`，但实时 `observation` 事件没有发送该字段；Demo 在 event reset 后重建观察列表时也只保留 bbox、label、note。

前端因而无法可靠实现：

- 按快照分组 observation；
- 只显示当前快照的 observation；
- 点击观察卡回到对应快照视角；
- 区分整视野小结与局部证据区。

### 2.5 正式 AI 标注只做了部分边界校验

`create_annotation` 和 PathTogether 写入端目前限制坐标非负、边长在 `1..40000`，但没有统一验证矩形右下角是否超出切片，也没有验证标注是否落在当前 pending snapshot 内。

这使正式标注也可能缺少“当前快照中确实可见”的几何证据约束。

## 3. 目标语义模型

本轮只定义以下三类对象：

| 对象 | 含义 | 数据来源 | 生命周期 | Viewer 表达 |
|---|---|---|---|---|
| 当前 AI 视角 | AI 最近一次实际获得并看见的快照范围 | `snapshot_captured` 的实际 bbox | 每个 AI 会话最多一个当前值；新快照替换旧值 | 青色虚线框；Viewer 导航到该 bbox 并留出边距 |
| 临时观察 | 本次会话中对某张快照的客观镜下记录，不写正式标注库 | `mark_observation` | 随会话保存；默认不把全部历史同时铺在 Viewer 上 | 整视野小结不画框；局部观察按需显示绿色框 |
| 正式标注 | 写入 PathTogether 标注库、等待人工审核的 AI ROI | `create_annotation` | 持久化，受权限、审计、审核状态约束 | 使用平台正式 AI 待审核标注样式 |

明确不引入第四类“扫描覆盖”对象。历史 snapshot bbox 仍可作为会话内部上下文存在，但本轮不制作覆盖热图、覆盖百分比 UI 或主视图覆盖轨迹。

## 4. 核心不变量

### 4.1 当前 AI 视角

1. `goto` 只是移动意图，不代表模型已经看见目标区域。
2. 只有成功的 `snapshot_captured` 才能更新“当前 AI 视角”。
3. 当前视角 bbox 必须采用平台实际返回的 `src/bboxLevel0`，不能采用裁剪前推算值。
4. 每个会话在 Viewer 中最多显示一个当前视角框；新快照直接替换旧框。
5. Demo 与正式插件使用同一导航口径：按实际 bbox `fitBounds`，四周外扩约 20%，让框完整留在视野内。
6. 当前有效的导航过程事件是 `tool_started {tool:"goto"}`：只用它更新“AI 正在移动”的轨迹状态，不据此导航或画框，等待随后 `snapshot_captured` 完成权威导航。
7. 现有 `type="goto"` 死分支不得继续依赖不存在的 `p.zoom`。兼容窗口内可保留为旧事件入口，但只归一化为轨迹状态，不承担 Viewer 导航；确认所有部署基线均不发送独立 `goto` 后再删除。

### 4.2 临时观察

`mark_observation` 增加显式 `scope`：

- `scope="viewport"`：对当前整张快照的客观小结。bbox 由服务端取当前 pending snapshot，不接受模型另报几何；前端只显示观察卡，不在 Viewer 画框。
- `scope="region"`：当前快照中的局部证据区。`x/y/w/h` 必填，校验通过后可在 Viewer 中显示绿色框。

其他规则：

1. observation 必须自动绑定当前 pending snapshot 的 `snapshot_id`，不信任模型自行指定其他 id。
2. Prompt 不再要求每张快照都必须产生 observation。
3. 纯导航、倍率确认、空白区或未见局部证据时，可以直接调用 `complete_snapshot_review(disposition="no_annotation", summary=..., no_annotation_reason="导航确认 / 未见明确局部证据")`。现有工具要求 `no_annotation_reason` 必填，本轮保留该审计语义，不放宽约束。
4. 只有存在客观、可定位的局部镜下证据时才使用 `scope="region"`。
5. “全片概览”“本视野主要为肺泡结构”等整视野描述使用 `scope="viewport"`，不能再生成铺在主视图上的大绿框。
6. Demo 中统一称“观察”或“观察区”，不得在总结和卡片里把临时 observation 称为“已标注”“第 N 处标注”。

### 4.3 正式标注

1. 只有 `create_annotation` 成功写入 PathTogether 标注库后，UI 才能称其为“标注”。
2. AI 创建的正式标注继续保持 `pending`，必须人工审核后才生效。
3. 正式标注必须位于切片边界内。
4. 正式标注必须位于当前 pending snapshot 内，保证模型写入的 ROI 来自其刚刚看到的证据图。
5. 越界时拒绝并返回明确、可修正的工具结果，不静默裁剪；静默裁剪会改变病理证据位置和范围。

## 5. 事件与会话契约调整

### 5.1 `snapshot_captured`

统一事件字段：

```json
{
  "snapshot_id": "tool-call-id",
  "bbox_level0": {"x": 100, "y": 200, "w": 4096, "h": 4096},
  "level": 2,
  "magnification": "5x（低倍，level=2）",
  "out_w": 1024,
  "out_h": 1024,
  "captured_at": 1787529600.0
}
```

现有事件只有 `snapshot_id/bboxLevel0/magnification/out_w/out_h`；`bbox_level0` 是兼容命名扩展，`level/captured_at` 是新增字段。`level` 从快照成功时的 `st.pyramidLevel` 取得，`captured_at` 由服务端生成。

兼容期同时保留现有 `bboxLevel0`，前端优先读新 snake_case 字段，缺失时回退旧字段。待 PathTogether 部署与外部插件 bundle 全部升级后再单独移除旧字段。

每次快照成功后，把以下轻量状态写入 Session：

```json
{
  "last_snapshot_view": {
    "snapshot_id": "tool-call-id",
    "bbox_level0": {"x": 100, "y": 200, "w": 4096, "h": 4096},
    "level": 2,
    "magnification": "5x（低倍，level=2）",
    "out_w": 1024,
    "out_h": 1024,
    "captured_at": 1787529600.0
  }
}
```

`last_snapshot_view` 只表示最后一次实际取景，不表示扫描覆盖。

### 5.2 `observation`

目标事件字段：

```json
{
  "snapshot_id": "tool-call-id",
  "scope": "region",
  "label": "局部腺样结构",
  "note": "结构较拥挤，需结合更高倍复核",
  "bbox_level0": {"x": 300, "y": 400, "w": 800, "h": 600},
  "magnification": "5x（低倍，level=2）"
}
```

`scope="viewport"` 时 `bbox_level0` 可以返回当前快照 bbox 供内部定位和卡片回跳使用，但前端不得把它画成观察 ROI。

### 5.3 Session UI snapshot

`GET session` 用于 event reset/reconnect 的返回中应包含：

- `last_snapshot_view`；
- observation 的 `snapshot_id`；
- observation 的 `scope`；
- observation 的 `bbox_level0`；
- 对应倍率说明。

Demo 重建状态时必须保留这些字段，不能再降维成只有 `x/y/w/h/label/note`。

旧 Session 兼容：

- 旧 Session 没有 `last_snapshot_view` 时，可从 transcript 最近一条有效 `image_ref.src` 推导；仍无法推导时不显示当前视角框，不伪造。
- 缺少 `scope` 的 observation 先尝试通过 `snapshot_id` 找到来源快照；bbox 与来源快照近似相同时按 `scope="viewport"`，明显小于且位于来源快照内时按 `scope="region"`。
- 无 bbox、零面积或非法 bbox 的旧 observation 按 `scope="viewport"` 读取，只显示卡片。
- 有有效 bbox、但无法找到来源快照的旧 observation 不直接认定为可信 region：保留观察卡，但默认不在 Viewer 画框。

滚动部署期间还必须兼容**旧实时事件**，不能只处理旧 Session：

```js
snapshotBbox = p.bbox_level0 || p.bboxLevel0;
observationBbox = p.bbox_level0 || p.bbox;
snapshotId = p.snapshot_id ||
  (state.currentSnapshotView && state.currentSnapshotView.snapshot_id) || null;
```

旧实时 observation 没有 `scope/snapshot_id` 时，套用与旧 Session 相同的推断规则。若既没有明确 `snapshot_id`，也没有当前快照可安全关联，则只显示卡片，不画局部框。

## 6. 几何校验规则

### 6.1 通用校验

所有 level-0 bbox 必须满足：

```text
x、y、w、h 均为有限数
x >= 0
y >= 0
w > 0
h > 0
x + w <= slide_width
y + h <= slide_height
```

正式标注使用正方形时：

```text
x + side_px <= slide_width
y + side_px <= slide_height
```

### 6.2 当前快照包含关系

对 `scope="region"` observation 和正式标注，再要求其矩形完全位于当前 pending snapshot bbox 内：

```text
tolerance_x = max(1, ceil(snapshot.w / snapshot.out_w))
tolerance_y = max(1, ceil(snapshot.h / snapshot.out_h))

region.x >= snapshot.x - tolerance_x
region.y >= snapshot.y - tolerance_y
region.x + region.w <= snapshot.x + snapshot.w + tolerance_x
region.y + region.h <= snapshot.y + snapshot.h + tolerance_y
```

容差表示快照输出图上的 **1 个像素** 映射回 level-0 后的误差，X/Y 必须按各自缩放比分别计算。不能只用 `bbox.w/out_w` 同时处理两个方向；边缘裁剪后的实际快照可能不是正方形。也不采用固定 0.5% 容差，因为在 1024px 输出下约等于 5 个输出像素，偏松且难以解释。

为支持上述校验，`PendingSnapshotReview` 除现有 `snapshot_id/bbox/image_ref` 外必须持久化：

```text
level
magnification
out_w
out_h
```

切片自身边界仍按 §6.1 严格校验，不使用快照容差放宽；动态容差只用于判断 region 是否有当前快照证据。超过容差则拒绝。

旧 pending snapshot 缺少 `out_w/out_h`，或输出尺寸非法时，动态容差按 0 处理（严格包含），不得猜测固定大容差；这允许旧会话继续完成判读，同时保持 fail closed。

拒绝结果必须向模型返回：

- 当前快照 bbox；
- 模型提交的 bbox；
- 哪一条边越界；
- 要求重新读取快照刻度并修正坐标。

不得自动把越界 observation/annotation 裁到快照边缘。

## 7. 前端状态与交互

### 7.1 Demo 状态拆分

现有单一 `state.observations` 之外，增加明确状态：

```js
state.currentSnapshotView = null;
state.observations = [];
state.selectedObservationId = null;
```

其中：

- `currentSnapshotView` 只由 `snapshot_captured` 或 Session 重建更新；
- observations 按 `snapshot_id` 保存；
- 当前快照切换后，历史 observation 仍保留在右侧轨迹/卡片中，但不全部画在 Viewer 上。

### 7.2 Viewer 绘制规则

1. 当前 AI 视角：青色虚线，无半透明大面积遮挡，标签显示倍率而不是模型自由填写的病理标题。
2. 临时局部观察：绿色实线；默认只显示当前快照下的局部 observation，或用户在观察卡中选中的一项。
3. 整视野小结：只显示卡片，不画框。
4. 正式 AI 标注：沿用平台待审核样式，与当前视角和临时观察明显区分。
5. 切片切换、新 run 开始、Session 明确重置时清空当前视角和临时高亮。
6. 标签做视口内约束；多个局部 observation 同时可见时需做基本上下避让。第一版通过“默认只高亮选中项”即可大幅降低重叠。

### 7.3 事件行为

- `tool_started {tool="goto"}`：显示轨迹状态，不创建框，不导航 Viewer，不把它当成模型已经看见的证据。
- 兼容期若收到旧 `type="goto"`：归一化成同一轨迹状态，不读取 `p.zoom`，不导航 Viewer。
- `snapshot_captured`：更新唯一当前视角框，导航 Viewer，追加快照卡。
- `observation(scope="viewport")`：追加观察卡，不画框。
- `observation(scope="region")`：追加观察卡；若属于当前快照，则显示或允许选中高亮。
- `annotation_created`：刷新正式标注数据；不复用临时 observation overlay 冒充成功状态。

### 7.4 正式插件对齐

正式 HistoPilot 插件已有按快照 bbox 导航和“只保留最后一个 overlay”的基础行为。本轮需：

- 使用与 Demo 相同的 `snapshot_captured` 字段兼容逻辑；
- 将 current snapshot view 与 observation highlight 分成两个状态，不再共用单个 `S.overlay` 数组表达不同语义；
- observation 卡点击后，通过 `snapshot_id` 恢复对应快照视角，再高亮局部 observation；
- 保证外部发布 bundle 与仓库 `integrations/pathtogether/ui/` 同步升级。

## 8. Prompt 与工具说明调整

### 8.1 Demo Prompt

把现有“每抓一张快照先用 mark_observation 记录”的要求改为：

1. 每张快照都必须调用 `complete_snapshot_review` 关闭。
2. 只有存在值得保留的客观所见时才调用 `mark_observation`。
3. 整张视野的结构性小结使用 `scope="viewport"`。
4. 可定位的局部证据使用 `scope="region"` 并提供 bbox。
5. 普通导航确认、空白或无局部证据时，以 `disposition="no_annotation"` 关闭，并填写 `no_annotation_reason`（例如“导航确认，未见需要单独记录的局部证据”）。
6. Demo 总结只能称“观察”“观察区”，不能称“已标注”。

### 8.2 正式 Prompt

正式 run 继续区分：

- `mark_observation`：会话级客观观察，不写正式库；
- `create_annotation`：确需人工复核的局部区域，写入正式库并进入 pending；
- `complete_snapshot_review`：关闭当前快照。

Prompt 明确禁止把低倍/概览小结自动升级为正式标注；正式标注应由当前快照中的具体局部证据支持。

## 9. 实施批次

### 批次 A：HistoPilot 契约与几何正确性

1. 扩展 `mark_observation` schema，加入 `scope`。
2. 增加 observation 与 annotation 的 slide/pending-snapshot 几何校验。
3. 扩展 `snapshot_captured`、`observation` 事件字段。
4. 为 `PendingSnapshotReview` 增加 level、倍率和实际输出尺寸，按 X/Y 各 1 个输出像素计算包含容差。
5. 持久化 `last_snapshot_view`。
6. 更新 Session snapshot/rebuild 数据。
7. 更新普通与 Demo Prompt，保留 `no_annotation_reason` 必填。

该批完成后，即使旧 UI 尚未升级，也不能再接受与当前快照无关的 observation/annotation。

### 批次 B：PathTogether Demo 视角修复

1. 让真实的 `tool_started {tool="goto"}` 显示轨迹状态；不据此导航 Viewer。
2. 移除现有死分支对 `p.zoom` 的依赖；兼容期旧 `type="goto"` 只归一化为轨迹状态。
3. 收到 `snapshot_captured` 后按实际 bbox 导航。
4. 增加唯一 current snapshot view 状态与青色视角框。
5. observation 按 `scope/snapshot_id` 渲染。
6. 整视野 observation 不画框；历史局部 observation 默认不全部铺开。
7. event reset 后按新 Session 字段完整重建。
8. 对旧实时事件与旧 Session 走同一字段归一化和安全降级逻辑。

### 批次 C：正式插件与正式标注对齐

1. 正式插件拆分 current view 与 observation highlight。
2. observation 卡支持回到所属快照并高亮局部区域。
3. PathTogether 正式标注写入端补齐切片右/下边界校验。
4. 联调 pending snapshot 内包含约束、AI pending 审核状态和标注刷新。
5. 构建并同步安装 HistoPilot 外部 release bundle。

### 批次 D：文案、兼容清理与手工验收

1. Demo 中清理“第 N 处标注”等临时观察误称。
2. 完成新旧事件字段兼容测试。
3. 观察线上/本地已存在旧 Session 的降级行为。
4. 待部署窗口内 PathTogether 与 HistoPilot 全部升级后，再另行决定是否移除旧 `bboxLevel0` 字段。

## 10. 测试计划

### 10.1 HistoPilot 单元测试

- `scope="viewport"` 不接受模型自报 bbox，自动绑定 pending snapshot。
- `scope="region"` 缺任一几何字段时拒绝。
- NaN、Infinity、零/负宽高拒绝。
- observation 超出切片或 pending snapshot 任一边时拒绝。
- X/Y 各 1 个输出像素映射后的动态容差边界通过，超过对应轴容差时拒绝。
- `PendingSnapshotReview` 正确持久化 level、倍率、out_w、out_h；旧数据缺失或输出尺寸非法时容差为 0，不以固定大容差兜底。
- annotation 超出切片或 pending snapshot 时不触达 PathTogether 写接口。
- `snapshot_captured` 包含 snapshot id、实际 bbox、level、倍率和输出尺寸。
- `last_snapshot_view` 在 snapshot 成功后持久化，失败快照不得覆盖旧值。
- Demo Prompt 不再要求每张快照都创建 observation，且不出现正式写入诱导词。

建议落点：

- `HistoPilot/test/tools.test.ts`
- `HistoPilot/test/session-store.test.ts`
- `HistoPilot/test/agent-runner.test.ts`
- `HistoPilot/test/demo-security.test.ts`
- `HistoPilot/test/message-sequence.test.ts`

### 10.2 PathTogether 前端测试

- `tool_started {tool="goto"}` 只追加轨迹，不导航 Viewer；旧 `type="goto"` 也不读取/调用不存在的 zoom。
- 连续两次 `snapshot_captured` 后只保留最后一个 current view。
- Viewer 最终导航 bbox 来自 `snapshot_captured`，不是 `goto` 推算。
- `scope="viewport"` observation 只产生卡片，不进入绘制列表。
- `scope="region"` observation 只有属于当前快照或被选中时才高亮。
- event reset 后恢复 last snapshot view、scope 和 snapshot_id。
- 新 UI 收到旧 HistoPilot 实时事件时，兼容 `bboxLevel0`（快照）和 `bbox`（观察）。
- 旧 observation bbox 与来源快照近似相同时推断为 viewport；无法关联来源快照时只显示卡片、不画框。
- 切片切换、新 run、Session reset 清空旧 overlay。
- Demo UI 不出现“已标注”误导文案。

建议落点：

- `PathTogether/tests/js/demo-ai.test.ts`
- `PathTogether/tests/js/plugin-sdk-bridge.test.ts`
- `HistoPilot/test/histopilot-renderer.test.ts`
- `HistoPilot/test/pathtogether-ui-boundary.test.ts`

### 10.3 PathTogether 后端测试

- 正式矩形标注右边界/下边界越界返回 400。
- 合法贴边标注通过。
- 失败请求不写数据库、不产生成功审计事件。
- Plugin Contract 与 legacy internal 路径使用同一几何规则。

建议落点：

- `PathTogether/tests/test_plugin_api.py`
- `PathTogether/tests/test_plugin_v1_transport.py`
- `PathTogether/tests/test_ai_fixes.py`

### 10.4 手工端到端验收

使用截图中同类任务完成一次只读 Demo 运行，至少检查：

1. 初始 snapshot 后 Viewer 导航到实际概览视角，只出现一个青色当前视角框。
2. “全片概览”或整视野结构小结只出现在右侧观察卡，不出现大绿色 ROI。
3. AI 进入低倍候选区后，current view 替换为低倍 bbox。
4. AI 进入高倍后，current view 再替换为高倍 bbox。
5. 只有模型明确记录的局部证据显示绿色观察框。
6. 历史低倍/高倍观察不默认全部叠在主视图；点击对应卡片可以恢复和高亮。
7. Demo 总结使用“观察区”，不称“标注区”。
8. 正式版只有 `create_annotation` 成功后才出现 AI 待审核标注。
9. 尝试提交当前快照之外的 observation/annotation 时被拒绝，且没有写入副作用。

## 11. 完成标准

以下条件全部满足后才能关闭本修复：

- Demo 与正式插件都以成功 `snapshot_captured` 的实际 bbox 作为当前 AI 视角权威来源。
- Viewer 中当前 AI 视角始终最多一个。
- 整视野 observation 不再画成绿色 ROI。
- 局部 observation 与其来源 snapshot 可追溯，且几何受当前快照约束。
- 正式标注与临时 observation 在数据、文案、颜色和审核状态上均可明确区分。
- event reset、刷新和 Session 恢复后语义不丢失。
- PathTogether 与 HistoPilot 的单元测试、契约测试和手工端到端验收全部通过。
- 外部 HistoPilot release bundle 已重新构建、安装并核对版本，不能只验证源码仓。

## 12. 明确不在本轮处理

- 扫描覆盖率统计、覆盖热图、历史视野轨迹展示。
- 模型病理判断质量、诊断准确率或取点策略的整体优化。
- 图片缓存、Prompt Cache、compaction 和视觉预算策略。
- 人工标注工具的交互重构。
- 将 Demo 临时 observation 写入正式标注库。

## 13. 实施记录（2026-08-24）

### 13.1 落地 commit

HistoPilot（master）：

| commit | 主题 | 对应 |
|---|---|---|
| `91f75b2` | feat(ai): observation scope 契约 + 快照几何校验 + last_snapshot_view | 批次 A |
| `95bc8bd` | feat(prompts): 快照消化节奏改为 complete_snapshot_review 收口 + scope 语义 | 批次 A（§8） |
| `4d7cb34` | fix(ai): GET /session 暴露 last_snapshot_view + SlideInfo.fingerprint 类型修复 | 批次 A 补充 |
| `b9ebfef` | fix(plugin): 视角/观察状态拆分对齐 Demo + config-panel DeepSeek 官方选项 | 批次 C §7.4（含 DeepSeek PR1 UI） |
| `53e0aeb` | Merge branch 'feat/plugin-viewport-align' | — |

PathTogether（main）：

| commit | 主题 | 对应 |
|---|---|---|
| `3516de7` | fix(demo): 当前AI视角唯一化 + observation 语义分层 | 批次 B |
| `027cd1b` | fix(annotation): 正式标注写入端切片右/下边界统一校验 | 批次 C 第 3 条 |

批次 D（本批收口）：两仓 Demo/插件面向用户文案核查（见 13.3）、方案文档状态更新与实施记录。

### 13.2 与方案的偏差汇总

1. **mark_observation 成功文案**改为「已记录整视野观察（viewport）：…/已记录局部观察区（region）：…」（`src/tools.ts`）；旧文案「已记录观察」在插件 `isSuccessfulToolResult`（`ui/renderer.js`）以正则兼容保留，三种文案均判成功（覆盖滚动部署期的旧会话回放）。
2. **create_annotation 的 `side_px`** 由「静默夹取 1..40000」改为越界**显式拒绝**并返回可修正错误（`src/tools.ts`：提交值回显 + 明示「服务端不会自动裁剪」）。
3. **observation scope「近似相同」推断阈值**：服务端 Session 重建（`src/session-store.ts:inferObservationScope`）实现为每边 `max(2px, 2%)`，且「完全覆盖来源快照」也归 viewport；前端兜底归一化（`static/demo.js` 与插件 `ui/renderer.js:classifyLegacyObservation`）为每边 `max(1px, 1%)`。三层路径方向一致（近似 → viewport、明显小于且包含 → region、无法归类 → 只出卡片）。
4. **PathTogether 标注写入端**在切片尺寸不可读时跳过包含校验（best-effort，与 `_rect_size_mm` 降级语义一致）；可读时右/下边界越界返回 400。
5. **旧字段保留**：`snapshot_captured` 的 `bboxLevel0` 与 `observation` 的 `bbox` 按方案 §5.1/§5.3 兼容期约定保留（前端优先读 `bbox_level0`，缺失回退旧字段）；移除待全部部署升级后另行决定（批次 D 第 4 条维持开放）。
6. **正式插件 overlay 颜色**受平台 `viewer.highlight` 单一样式限制：视角框/观察框暂无法用不同颜色区分，本轮以文案与交互区分（观察卡 scope 标签、点击回到所属快照并高亮）；多样式需求另行排期。

### 13.3 批次 D 文案核查结论（2026-08-24）

全仓 grep（`已标注|标注区|处标注|个标注|标注了`）核查 `PathTogether/static/demo.js`、`static/i18n.js`、`templates/`、`HistoPilot/integrations/pathtogether/ui/`：

- 唯一命中 `i18n.js:505`「请先用 ROI 框选区域或选中一个标注」——指平台真实标注列表的合法用法，非临时 observation 误称，保留；
- 其余命中均为代码注释（如 `renderer.js:494` 的约束说明），非用户可见文案；
- 观察语义文案已统一为「观察 / 观察区 / 整视野观察 / 局部观察区」（`i18n.js:302-305,573`、插件 `bridge-client.js:52-54`）；
- `tests/js/demo-ai.test.ts:860-866` 已有断言守护：Demo 源码与 i18n 值不得出现「已标注 / 第 N 处标注 / 标注区」。

结论：无残留，无需修改。
