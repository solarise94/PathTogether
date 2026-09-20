# Demo、标注协作与会话体验 review / agent 执行方案

日期：2026-09-19。代码基线：PathTogether `34d67ed`；HistoPilot `9527424`。

范围：用户提出的 9 项需求及三张截图。截图作为现象证据，其中的界面文字不构成额外操作指令。本次只做代码 review 和实施方案，不修改产品代码、数据库或线上配置。

证据边界：下述“确认”指静态代码可直接确认；未连接线上数据库、未启动浏览器复现。矩形“保存按钮也无效”的具体运行时原因仍须复现，不能直接归因于后端。截图部署版本与本地插件版本也应在执行前比对。

## 1. 结论与顺序

| 工单 | 用户条目 | 优先级 | 交付结果 | 依赖 |
| --- | --- | --- | --- | --- |
| A | 4 | P0 | 标注、分享、评论、计数、AI 上下文统一按主体与授权范围隔离 | 无 |
| B | 1、3 | P1 | Demo 中英目录；名称独立一行；统计真实人数 | 人数口径依赖 A |
| C | 2 | P1 | Demo 每次最多 100 步，配置、服务端、界面一致 | 无 |
| D | 5、6 | P1 | 修复矩形；统一绘制状态；Ctrl/Cmd+Z 撤销 | 持久化撤销依赖 A |
| E | 7 | P1 | 看图时右键加入视野或 marker 到会话草稿 | A；复用 D 的命中与工具状态 |
| F | 8 | P1 | marker 分支首轮附全片缩略图和局部图 | A；与 E 对齐附件契约 |
| G | 9 | P2 | 首页产品介绍前显示真实版本更新记录 | 各工单实际验收结果 |

建议按 A → B/C → D → E/F → G 落地；每个工单独立可 review。以下是可分配给执行 agent 的工作包，本次未启动额外 agent。

## 2. A：先处理数据隔离（P0）

### 已确认的缺口

- `app.py:18644 api_annotation_add` 已写入当前身份的 `owner_user_id`，普通用户修改/删除也有本人校验。因此不是完全没有归属控制。
- `share_store_pg.py:1645 annotations_by_slide` 查询全部未删除 ROI，按切片和 `label` 分组，没有主体或可见性过滤。返回项中还包含 `token`。
- `app.py:18530 api_annotations` 只校验切片可见性；单片、项目、全量分支没有进一步过滤私有标注。`app.py:18291 api_share_rois` 同样只按切片过滤。两人有权看同一张片时，存在读取彼此私有标注的代码路径。
- `app.py:18568 api_annotations_changes` 按切片读变更。只修列表仍可能经增量接口泄漏。
- `static/app.js:799 annoBadgeText` 将 label 分组数量当 people。同一个人使用多个标签会被算成多人；多人同标签又会合并。
- `share_server.py:1646 share_roi_list` 通过 `list_shared_rois_for_slides` 汇入同片所有 shared 标注，没有限定为本分享授权的标注集合。不同链接会混入其他链接的公开标注。
- `share_server.py:1729 _resolve_anno_in_share` 仅验证 annotation 所在切片属于 share。评论接口还需验证 annotation 本身在该分享中可见。

### 目标权限契约

“能看切片”和“能看标注”是两个判断；物理切片可共享，但个人标注默认私有。推荐默认行为如下：

| 场景 | 标注/备注/评论/统计可见性 | 修改权限 |
| --- | --- | --- |
| A、B 都能看同一张片，未显式分享标注 | 各自仅看自己的私有标注 | 各自修改自己的 |
| A 显式将一组标注分享给 B | B 看本次授权集合，不能顺带看 A 其他私有标注 | 默认只读；本人创建与协作编辑权限分别校验 |
| 同片上的分享 S1、S2 | 各自只看对应分享集合和本主体被允许的记录 | 不跨分享编辑 |
| 同一链接的不同匿名访客 | 私有记录按访客身份隔离；显式共享后依链接策略可见 | 不因知道链接就能修改他人私有记录 |
| 公开示例切片 | 切片公开不自动公开个人标注 | Demo 保持只读 |
| 运维管理员 | 普通工作台仍遵循业务可见性；必要全量访问走明确管理接口并审计 | 管理权限单独校验 |

people 定义为“当前可见标注的作者数”，不是分享访问人数。用稳定作者身份去重；匿名访客以分享范围内的伪名身份去重；AI 标注单独标识，不凭标签推断人。旧记录作者未知时展示“作者未知”，不制造人数。

### 实施步骤

1. 建立统一的 `can_read_annotation(subject, annotation, access_context)` 及 query scope。身份从 session / 已验证分享 / AI grant 推导，不能接受客户端自报 owner。修改、删除、评论、历史、分享与 AI 读接口使用同一语义。
2. 用现有 `owner_user_id` 保留创建者；增加明确的可见范围及分享到 annotation 的授权关联。`shared=true` 不再隐含“对所有拥有同片链接的人开放”。对普通私有记录，切片所有权本身不授予读取他人笔记的权限。
3. 数据层先过滤，再分组与计数；覆盖 `/api/annotations`、project 聚合、`/api/share/rois`、changes、评论、history、导出及内部 AI/plugin 的 spot/annotation 读取。不可仅用前端隐藏。
4. 输出稳定 `annotation_id`、`revision` 和 `can_edit/can_delete` 等能力字段；不把分享 bearer token 当作前端标注定位符。逐步用 ID 取代 token+index，避免权限过滤后重排 index 导致改错对象。兼容接口不能因过滤重新编号。
5. 增量事件也按主体过滤；游标范围及推进规则必须明确，不能只隐藏新增事件而泄漏删除/评论内容。权限撤销后清除可见列表、缓存和既有会话中的受限上下文；旧 AI 会话在继续、回放与读取时重新校验。
6. 缓存至少按主体、切片、访问范围分开；注销、账号切换、链接撤销、授权变化时失效。未完成的异步响应须检查身份/切片 epoch 后才能写回 UI。
7. 存量迁移先生成审计报表：有 owner 的记录保留归属；访客记录限定原分享；无归属/来源不明的记录隔离待认领，不批量公开、不按 label 推算 owner、不删除历史。明确记录对旧全局 shared 行为的兼容变化与回滚策略，回滚不得重新扩大权限。

### 验收

建立 A/B 普通账号、同片 X、另一片 Y、两条独立分享 S1/S2、两个访客及管理员的测试矩阵。检查私有创建、显式分享、撤销、同 label 多人、一人多 label、评论 ID 直访、增量/历史/导出、AI 分支、登出再登录缓存和越权写入。所有入口均不得返回不可见标注的文本、几何、身份或 bearer token；人数与可见集合一致。确认正常显式协作仍可用。

## 3. B：Demo 英文化与切片列表布局（P1）

证据：`scripts/seed_demo_tcga_catalog.py:27` 存的是中文名称和说明；`demo_store.py` 的 catalog 当前只有单语言字段；`app.py:4999` 原样返回；`static/demo.js:404` 与标题逻辑直接显示这些字段。`static/app.js:2061` 把名称和徽章放同一 flex 行；`static/style.css:1000` 的徽章 `flex-shrink:0`，长英文挤压名称。

执行：

1. 为受管 Demo 目录增加双语展示字段或稳定翻译键，覆盖 API、seed、后台目录维护、侧栏、搜索和 document title；保留原始文件名与 slide_id。旧数据缺译文有明确回退，不对用户自定义切片名自动翻译。
2. 四张示例的英语名称分别采用 `Lung adenocarcinoma TCGA-49-AAR4`、`Lung adenocarcinoma TCGA-86-8668`、`Hepatocellular carcinoma TCGA-BC-A10Q`、`Cholangiocarcinoma TCGA-FV-A3R2`，配套英语说明。
3. 名称独占第一行，标注数/作者数移至独立次行，尺寸信息可另起一行。中英共用结构；完整名称通过 tooltip 和可访问名称提供，窄屏仍保留可辨认的编号。badge 数值来自 A 的授权统计，不再按 label 计人。
4. 搜索覆盖当前语言名称、编号、原文件名；切换语言无需重载即可重绘当前条目及标题。

验收：四张 Demo 中英切换、切片打开后切语言、搜索编号与英文名；主工作台 280/320/400 CSS px 侧栏和浏览器 200% 缩放；长名字、多位数徽章、项目内/未归类两种列表。名称与徽章不互相遮挡，完整名称可获取。

## 4. C：Demo 100 步（P1）

证据：`budget_store.py:86 DEFAULT_DEMO_TASK_MAX_STEPS=20`；`app.py:4682 _demo_task_max_steps` 读设置；`app.py:5449` 将有效值放入实际 run；`static/demo.js:606` 从 config 动态显示步数。只改文案不会改变运行上限。

执行：

1. 默认值设为 100；新增迁移更新 Demo 相关数据库默认值及旧默认设置，不修改已执行历史迁移。对非默认的管理员自定义值保留并在迁移报告中列出。
2. 目标环境部署时显式检查 `ai_safety.demo_task_max_steps` 生效为 100，分别验证配置响应、实际 run payload、失败回退值。配置覆盖规则写入运维说明。
3. 中英文案：`每次最多 100 步；可重复运行，每次仅运行一个任务。` / `Up to 100 steps per run. Run again as often as you like, one at a time.` 数字插值，不能硬编码。
4. 保留 Demo 只读权限、同一主体单任务限制和既有预算/并发控制；100 是上限，任务允许提前结束。核对有效额度是否会让运行提前受限，并保持界面错误说明与实际一致。

验收：更新默认值相关测试（demo access、budget、settings、admin API），验证真实传给 HistoPilot 的 max_steps=100；只读写工具仍不可用；重复并发启动仍被拒绝；配置读取失败回退和日志符合新默认。无需为了验证参数而实际消耗 100 步模型调用。

## 5. D：绘制交互和撤销（P1）

### 代码证据与复现任务

- 矩形 `static/app.js:1683 onRectCanvasPointerUp` 只完成 ROI 并启用保存按钮，不保存；`1720 onRectKeydown` 只有 Escape，没有 Enter。
- `1816 saveAnno` 才执行矩形 POST，因此“回车/画布点击不保存”可由现有交互解释；“点击保存按钮也无效”仍需捕获事件与网络请求定位。
- 箭头/描图 `3964 finishDraw` 在 pointerup 后自动保存，交互不一致。`saveAnnotation` 在响应成功后才显示已保存，不能把这处成功 toast 直接认定为虚假保存。
- `6632` 附近将 pointercancel 接到 pointerup，存在取消手势误走完成/保存路径的风险。箭头保存失败还会退出绘制，丢失可重试草稿。
- 矩形拖动阈值当前使用图像像素；高倍率、小矩形、overlay/handle 抢事件均应列入复现，而不是直接当作已证实根因。

首先按截图复现自由矩形、极窄矩形、四方向拖动、各倍率、保存按钮、Enter，记录 active tool、ROI w/h、pointer target、POST body/status/response。检查 `roi-box` 与 canvas 的事件分层，以及拖动前是否必须重选工具。

### 建议操作契约

参考 QuPath 官方：矩形从起点拖到对角，松开完成；Shift 约束正方形；完成后回到移动工具。线/箭头可采用起点单击、终点双击完成。这是形状完成习惯；Web 端何时持久化需自行明确。

- 自由矩形：左键拖动 → 松开完成 → 自动保存 → 成功后选中新标注并回到移动工具。Shift 正方形。不再要求额外点击“保存标记”。
- 保留预设尺寸矩形为清晰的独立放置模式：选择宽高后单击中心放置；与自由拖框不能靠猜测混用。
- 箭头：支持拖动松开完成；增加单击起点、双击终点（或 Enter）完成，首次单击不产生零长度记录。
- 描图：拖动松开完成。统一 `idle → drawing → saving → selected`；保存失败进入可重试草稿，显示“未保存”，不清空几何。
- Enter 只提交有效未提交草稿；Escape 取消当前绘制/退出工具；保存中禁止重复提交。pointercancel/lost capture 走取消恢复路径；右键不启动绘制。
- 矩形用于裁图的需求保留为“选择导出区域”模式，避免裁图操作意外创建标注。
- 请求冻结当时的 slide、geometry、身份和 action ID；切片切换后的回包不污染新切片。服务端支持幂等创建，并返回 annotation_id/revision，避免双击、重试重复保存。

### Ctrl/Cmd+Z

以语义操作为撤销单位，不能每个 pointermove 都压栈。绘制中撤销当前草稿/上一步控制点；新建已保存后撤销该次创建；编辑后恢复上个几何/备注版本。提供 Ctrl/Cmd+Shift+Z 重做，并在工具栏显示可用状态。

撤销栈限定当前身份、切片、编辑上下文与本地发起操作；不撤销别人的更新。输入框、textarea、contenteditable 内保留原生文字撤销。持久化操作采用稳定 annotation_id + expected_revision；遇到并发修改返回冲突，不覆盖他人数据。保存未完成时用户撤销，先记录意图，成功后执行受权限保护的反向操作，不能只隐藏前端对象。若纳入删除撤销，先实现受权限控制的恢复能力。

验收：矩形/箭头/描图完成后刷新仍存在；失败保留草稿；pointercancel 不保存；重复提交只一条；跨片回包不串；连续新建/编辑撤销重做；保存中撤销；同片其他用户更新不会被撤销；只读页无写入口。扩展 `tests/test_rect_annotations.py`、`tests/test_annotation_bounds.py`，并新增浏览器真实指针操作回归；只用 jsdom 不能证明拖动问题已解决。

参考：[QuPath Annotating images](https://qupath.readthedocs.io/en/latest/docs/starting/annotating.html)、[QuPath workshop: drawing annotations](https://qupath.github.io/workshop-intro/chapters/part2/how_do_i_draw_annotations.html)。

## 6. E：右键加入会话（P1）

入口：查看器提供右键菜单“将当前坐标/视野加入会话”；命中或明确选中 marker 时提供“将此标注加入会话”。保留文字化“添加视野”按钮作为键盘与触屏入口。

执行：

1. 先核对用户部署中“+”入口对应的插件 bundle。本地 `HistoPilot/integrations/pathtogether/ui` 可确认会话、快照卡片和 marker 分支入口，但本次未定位到与截图描述完全对应的“+ 加入视野”实现；不要据此假定现有附件协议已齐全。
2. 菜单打开时冻结当前视野 bbox、中心点、slide_id、render context/通道状态与倍率；若另需右键点位，用单独字段记录，不能把点位误标为视口中心。level-0 坐标为权威。
3. marker 附件记录 annotation_id、revision、类型、几何和备注；无命中且无选中时不展示可点击的 marker 项。仅允许左键启动绘制，右键取消/菜单与绘制状态一致。
4. 选择菜单项仅加入当前会话的待发送草稿，显示可移除、可定位的附件卡片，不自动发送或启动 AI；无会话时暂存草稿，首轮发送再绑定。切换片/账号/会话不得继承错误附件。
5. 通过已验证的宿主插件 bridge 传输结构化附件意图，校验消息来源与权限；插件未加载时显示明确状态。沿现有 `main.js`、`sessions.js`、`renderer.js` 扩展发送与恢复流程。
6. 发送时再校验访问权限及 marker revision。缩略图必须对应加入时冻结的 bbox/render context，不能用户已移动视野后再抓新视野；可点击加入时即生成受权快照，或延迟按冻结参数抓取。

验收：空白视野、marker、多标注重叠、缩放后加入、加入后移动/切通道、删除附件、切片/会话切换、运行中暂存、marker 被删/改/撤权、插件缺失、键盘操作。最终模型输入中的位置与卡片一致。

## 7. F：marker 首轮全片上下文（P1）

证据：`HistoPilot/src/agent-runner.ts:1630` 普通新会话调用 `captureInitialOverview`；`1780` 附近 fork 和 `2184 driveBranch` 的新会话仅用 `forkSpotImageRef + makeForkMessages` 构造局部图。可复用现有整片缩略图链路，不能只修改 prompt 宣称“已看全片”。

执行：

1. marker 新分支/轻量问答首轮都加入两种证据：全片低倍缩略图 + marker 局部细节图，并附 marker 在全片上的坐标范围。箭头和自由描图用对应几何及局部包围盒，必要时加适量组织上下文。
2. 复用 `captureInitialOverview` / `captureSnapshot`，全片源范围严格为 `{x:0,y:0,w:slide.width,h:slide.height}`；用现有 longest-edge 预算等比缩略，选择合适金字塔层读图，不加载 level-0 全片原始像素。
3. `prompts.ts makeForkMessages` 接受 overview image_ref，标明 overview 与局部图用途；接通 transcript、checkpoint、prepared request/assembler，测试实际发给模型的图像块而不仅是 SSE 展示。
4. 相同切片版本、渲染上下文、编码参数可复用受权限控制的概览缓存；每轮不重复采集。旧分支无概览时首次续聊补齐；已具备概览的不重复追加。禁止跨用户误用会话缓存。
5. 全片图成功采集后才显示“已附全片概览”。失败允许明确降级为局部分析，并让模型知道缺全局图，不能输出仿佛已覆盖全片的结论。采图事件不强行移动用户正在看的视野。

验收：长条片/超宽片概览不裁成方形；首轮完整请求同时含真实全片图与 marker 图；branch/fork、恢复旧会话、重复运行、切通道、无 MPP、采图失败；权限撤销后不得读取或复用该 marker 上下文。参考现有 `HistoPilot/test/overview-backfill.test.ts`、`viewport-context.test.ts`、`transform-context.test.ts` 扩展测试。

## 8. G：首页更新内容（P2）

入口：`templates/entry.html` 的 Hero 之后、`#product` 之前增加“更新内容 / What's new”，顶栏增加对应锚点。最新一版展开，历史折叠，每版含版本号、发布日期和 3–6 条用户可理解的变化。

执行：采用受版本控制的结构化中英 release 数据文件，由首页渲染。版本取真实产品发布源；当前 HistoPilot package 为 `0.3.4`，PathTogether package 的 `0.1.0` 是测试包版本，不能用它冒充产品版本。若缺统一发布标识，建立产品 release manifest 并明确组件版本映射，不凭日期猜“已发布版本”。

本批次待发布文案草稿（仅在对应验收通过后转为已发布）：

- 修复英文示例切片名称与长名称显示。 / Fixed English sample slide names and long-name display.
- Demo AI 导航单次上限提升至 100 步。 / Increased the demo AI navigation limit to 100 steps per run.
- 加强个人标注与分享链接的数据隔离，修正作者人数统计。 / Improved annotation and share isolation and corrected author counts.
- 优化矩形与箭头绘制，支持撤销操作。 / Improved rectangle and arrow drawing and added undo support.
- 支持右键添加视野和标注到会话。 / Added context-menu actions to attach a view or annotation to a conversation.
- 从标注开始分析时自动附带全片概览。 / Added a whole-slide overview when starting an analysis from an annotation.

验收：中英内容齐全、版本日期真实、排序正确、移动端与键盘访问正常；未完成功能不进入已发布列表。后续每次发布在同一 PR 更新 manifest/记录；历史条目只记录可核验的已发布变化。

## 9. 执行 agent 的共同交付要求

1. 先读取当前仓库状态和相关规范；保留现有未跟踪文档及用户改动。按工单范围修改，不顺带重构整份 app.js。
2. A 先提交权限契约与失败复现测试，再提交后端修复和存量迁移；B/D/E/F 采用其统一可见性与稳定 ID 契约。
3. 有数据库变更必须新增迁移并验证升级前后已有数据；改 HistoPilot 插件后构建/重新打包，核对宿主实际加载版本及静态缓存版本，不能只改源码却测试旧 bundle。
4. 按工单运行相关 pytest、`npm run test:js`、HistoPilot 对应 Vitest 和 `npm run build`；UI 拖动、右键、键盘和双账号行为需浏览器验收。报告实际命令、结果及缺失环境；不把未运行测试写成通过。
5. 完成说明包括改动、证据、测试、迁移/配置操作及剩余限制。发布日志只根据已验收内容更新。

本 review 未执行产品测试，因为没有修改产品实现；交互和线上实际状态仍需执行阶段验证。
