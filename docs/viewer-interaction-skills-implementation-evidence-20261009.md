# Viewer 交互升级与描绘按需技能：实施与验收记录

日期：2026-10-09/10（Asia/Shanghai）。状态：**实施完成、门禁通过；未推送、未部署、未发布 HP**。
设计：`docs/viewer-interaction-skills-review-design-20261009.md`（5328fefc）。
契约：`docs/viewer-drawing-skill-contract-20261009.md`（描绘技能，跨仓）、
`docs/viewer-interaction-lock-contract-20261009.md`（位置锁）。

## 1. 分支与提交

| 仓 | 集成分支 | 头 | 说明 |
| --- | --- | --- | --- |
| PathTogether | `viewer-skills`（基 5328fefc） | 见 `git log`，本文件所在提交 | 合入 vi-chrome / vi-lock / vi-skillui / vi-skill / vi-anno + 主代理修复 |
| HistoPilot | `hp-viewer-skills`（基 release/hp-0.3.5 = 23e89fa） | `48601f9` | 合入 hp-skill（服务端）+ hp-skill-ui（插件 UI）；版本 0.4.0 / pluginContractVersion 1.1.0 |

PT 子分支：`vi-chrome` 8d46e7c8（A 标题整栏拖动 + B 无黑幕交接）、`vi-lock` 86148e5b（D 位置锁后端）、
`vi-skillui` 02fa4746（HP 桥 `viewer.getSkillTarget`）、`vi-skill` 4ea5f2f5（E 0082 + /skills + 写入口改查 run grant）、
`vi-anno` c7355939（C polygon 完整性、选择/路由/工具条、D 锁前端与差量保存）。

主代理在集成分支上的修复：
- `c6e8bd1e` **既有生产缺陷**：`_resolve_anno_by_id` 只走名通道，P2 后所有 id_bundle 切片（无 legacy 名）
  的工作台 by-id PATCH/DELETE/restore 一律 403。改为有 slide_id 且无 legacy 名时按
  `_desc_can_annotate` / `_authorize_or_deny` 判定。回归 `tests/test_annotation_by_id_id_bundle.py`
  在修复前 2 failed（403「无权访问」），修复后 3 passed；他人仍被拒。线上撤销与备注保存同受此缺陷影响。
- `16d67865` / `a258d37e`：取消 by-id 持久化 e2e 的 fixme，补真实工具条「锁定位置」与刷新保持；
  保存失败改用模拟 503 覆盖「未保存 · 重试」；e2e 清理按 annotation_id，避免全量套件 tombstone 导致 index 错位。
- `9ac87176`：迁移台账测试补 0082。

## 2. 设计落实要点

- **A 标题整栏**：整条 header 起拖（含按钮），6px 阈值；超阈值才捕获指针并只抑制本次手势的 click；
  Esc/pointercancel/失焦回起点；rAF + transform，结束一次提交与持久化；≤768px 固定；
  「重置面板位置」移入 ⋯。
- **B 翻牌交接**：删除不透明 `.slide-deal-stage`；换片前冻结上一张已解码预览为背景桥（不读回画布）；
  新预览 decode 后才退场旧层；就绪 = 底图已解码可见或瓦片网格覆盖可见区域 ≥98%；
  OSD `open()` 内部的同步 close 不清桥接层，显式清屏（到期/失败/无切片）才清。
- **C 标记**：polygon 在主站与分享页的绘制、命中、定位、编辑、序列化全路径补齐，类型不改写；
  选中不隐藏其它标记，单一蓝轮廓；就近工具条四种形态并避开标签；捕获阶段路由，只在确认编辑时消费事件；
  **选中后可直接拖控制点**；旋转/镜像矩形按四顶点投影；松手一次 PATCH；方向键合并为一个撤销单元。
  顺带修复既有缺陷：freehand/polygon 内部命中恒假（点列格式不符）。
- **D 位置锁**：`rois.data.geometry_locked`，无迁移；行锁内先查锁后查 revision；
  锁与几何同请求 400、锁切换须带 expected_revision；已锁改几何/删除 409 `annotation_locked`；
  前端按错误码区分锁定与版本冲突，备注保存不再夹带 geom。
- **E 描绘技能**（2026-10-10 已按 §7 修订，下述判定方式作废）：checkbox 全链移除；`@描绘` 候选与可删标签、自然语言预判标签；
  意图判定唯一权威在 HP，**纯本地确定性规则**（本轮不调模型），只读本次原始用户文本；
  判定成立后 HP 调 PT `POST /api/plugin/v1/run-grants/<id>/skills`（只 false→true，事务内复查
  grant 状态与创建者当前写权）；run_grants 0082 `allow_ai_drawing` 是两条写入口（plugin v1 与
  internal）的唯一授权依据，**无过渡窗口**，旧会话开关无效力；新 run 不继承；
  冻结 target 进入任务前缀，描绘结果与 target 无交集则软拒绝；`skill_status` SSE 事件
  （assembled / unavailable+稳定 reason）。

## 3. 门禁（主代理在集成分支执行）

| 门禁 | 结果 |
| --- | --- |
| PT pytest 全量（不忽略文件） | 2981 passed / 113 skipped / 4 failed → 4 项均已处理：0082 台账（已修，9ac87176）；3 项「平台仓不内置 HistoPilot」断言因验收时临时放入 `plugins/histopilot` 而失败，移除 bundle 后该 3 文件 43 passed |
| PT vitest `tests/js` | 1115 passed / 2 skipped |
| PT Playwright 全量（真实 Flask + 内嵌 PG + 新 HP bundle 0.4.0） | 112 passed（首轮 1 失败为 e2e 清理按 index 的测试缺陷，已修复后全过） |
| HP `npm test` | 80 files / 1693 passed |
| HP `npm run test:contract`（指向 viewer-skills PT checkout） | 49 passed |

关键证据（子代理生成，未提交，位于各工作树 `.gate-tmp/`）：
- 翻牌逐帧（CDP screencast，组织区采样）：基线暖缓存重开 11/54 帧全黑、A→B→C 26/69 帧全黑；
  修复后均 0；冷缓存/扣 info/扣首瓦片/缩略图 decode 延迟均 0（冷首开 1 帧为允许的空态底色）。
- 回归在基线失败：位置锁后端 15/16、标记前端 79/80、描绘技能 PT 31 + HP 13/4/4、
  意图规则探测 12、选中即可拖柄 5。
- 主代理目视：基线黑幕帧 vs 修复后连续底图；选中态单蓝轮廓 + 8 柄 + 工具条不遮标签；
  @ 候选、「描绘 · 本次」标签、「已生成 2 个待复核标记 · 定位」。

## 4. 未做 / 已知限制（不冒充通过）

- **未调用真实模型**：描绘技能的「判定 → 置位 → 装配 → 模型产出 polygon → 落库」在契约测试中以真实
  PT + 真实 HP 客户端栈验证 /skills 与写入口闸门，但模型输出为桩；真实推理 dogfood 需付费 AI，
  未执行。浏览器端「正在描绘……」全链 SSE 未在 e2e 中端到端跑（e2e 无 AI sidecar）。
- 未做 Mac 实机 / 2×DPR 帧率测量；8 秒超时路径无专门 e2e（代码审查 + 单元覆盖）。
- ~~意图判定为枚举规则，误判方向为少授权~~：该说法不成立（复核实测否定句与提问被判成授权），
  规则已于 2026-10-10 移除，见 §7。
- branch（askBranch）路径不装配描绘工具；HP 重启后恢复的 run 不恢复技能授权（fail-closed）。
- 「分享携带已有标记」独立问题（设计 §9）仍未解。
- 位置锁「锁 + 备注」同请求在后端为两次事务（非原子）；前端始终单独发送锁切换。

## 5. 上线前提（需用户逐次批准）

1. PT 迁移 0082（幂等 ADD COLUMN），在生产 schema 副本演练。
2. PT 与 HP **同一 suite 镜像一起部署**：旧 HP + 新 PT 描绘不可用（fail-closed），新 HP + 旧 PT 同样不可用；
   任何组合都不会回到永久开启。HP 需发布 0.4.0 bundle（UI 改动不能只靠 PT CSS）。
3. ~~本分支基于 admin-viewer 5328fefc，包含第四轮未部署内容（0081、插件 0.4.17、APP_REVISION 等）。~~
   更正（2026-10-10 复核只读核对 homepc）：生产已运行 PT `5b87fb90`、admin 插件 0.4.17、迁移台账到 0081。
   本分支相对生产新增的是 0082 与本轮 PT/HP 改动。
4. 部署形态以契约版本兼容为准：新 PT + 旧 HP、旧 PT + 新 HP 均为描绘不可用（fail-closed），其余功能照常；
   不以「必须同一镜像」作为前提。HP 根 package.json 已与插件 manifest 对齐为 0.4.0。

## 6. 追加：切片抽卡动画改为「文件袋抽出 → 翻面 → 铺开」（2026-10-10，主代理实现）

用户反馈侧栏悬停与开片动画像「浮起来」。改动只涉及表现层，网络与交接逻辑（§4）不变：
- 悬停：卡片以原尺寸从叠中**横向抽出** 72px，相邻卡随高斯衰减少量跟随；不再放大到 240px、上抬 28px、倾斜 -3°。
  只保留视口钳位（右缘无空间不移，底部越界上移到 16px 边界）。
- 点击：飞牌正面是侧栏文件卡的原样克隆（按侧栏尺寸排版后缩放，起点逐像素一致）；
  ①0–34% 原尺寸水平抽出越过侧栏边缘（带 -1.5° 手势倾角）；②40–92% 飞向视窗中心并绕竖轴翻面 180°，
  背面即切片预览图；③铺开阶段沿用原逻辑把同一张已解码图放大到视窗。总时长 700ms（原 620ms），
  请求仍与动画并行。删除了蓝色「HistoPilot」牌背与 rotateZ 摆动。
- 验证：CDP 0.1× 慢放逐帧截图人工看过（抽出、侧立、翻面、铺开、交接后无残留）；
  `sidebar-pullout.test.ts` 旧几何断言（右移 40/上抬/放大 240）按新契约改写；vitest 1115 通过；
  Playwright 全量 112 通过（含黑帧逐帧检测仍为 0）。

## 7. 修订：描绘意图改由模型判断（2026-10-10，主代理实现）

依据：用户决定「模型判断本轮描绘意图，后台检查写权限，授权不跨轮继承」，不再补关键词规则；
复核报告 `docs/viewer-skills-codex-review-20261010.md` §1 的否定、提问案例保留为模型验收集。

- **删除关键词判定**：HP `src/skill-intent.ts` 及其句子表测试删除；UI 删除自然语言预判与「描绘 · 本次」
  自动标签，删除标签不再发送 `dismissed_skills`（服务端仍尊重该键）。
- **工具提供**：可写 main run（有 grant、非只读、非 ephemeral）一律提供 `draw_suspicious_region`；
  工具说明只描述环境（待复核标记、用于本轮要求、之前轮次不延续、提问/讨论不需要画、@描绘 即明确要求），
  系统提示不变，不强制画图。
- **后台检查写权限**：模型本 run 首次描绘、几何校验通过后、写入前，HP 调 `/skills`（PT 事务内复查 grant
  与创建者写权限）；同 run 只调一次。失败则不写入，工具回说明，`skill_status: unavailable`；
  成功 `skill_status: assembled`（UI 显示「正在描绘……」）。写入口仍只认 grant 列。
- **不跨轮**：新 run 新 grant；runner 测试覆盖「上一轮画过、本轮不画 → 不再置位」。
- **选中范围**：浏览器每条 run/continue 附带发送时的选框/已有标注（不附视野/整片），HP 加一行
  `【发送时选中】…` 上下文；取消「描绘与 target 无交集即拒绝」的范围闸。
- **来源**：`/skills` intent 与 provenance `skill_intent` 接受 `explicit | model`（兼容旧 `natural`）；
  0082 列无约束，仅改注释。
- **成本与误判**：不新增模型调用。模型仍可能多画或漏画，不承诺误判方向；结果均为待复核标记。

门禁（2026-10-10）：

| 门禁 | 结果 |
| --- | --- |
| HP `npm test` | 80 files / 1513 passed（句子表测试随规则删除） |
| HP `tsc -p tsconfig.build.json --noEmit` | 通过 |
| HP `npm run test:contract`（真实 PT，含 intent=model 置位与 provenance 落库） | 49 passed |
| PT pytest：描绘闸门文件 | 72 passed（新增 model 来源 2 项） |
| PT pytest：涉及 run grant / skill / 迁移台账的 29 个文件 | 551 passed |
| PT vitest `tests/js` | 1115 passed / 2 skipped |
| PT Playwright 全量（临时装入新 HP bundle，跑完已移除） | 114 passed |

未做：
- **未用真实模型运行验收集** `HistoPilot experiments/evals/drawing-intent-v1.json`（35 条单轮 + 3 组多轮，
  含复核报告全部反例与「帮我识别并圈出腺体」「分别圈出两个区域」）。运行需付费调用，须记录模型版本与逐条结果。
  runner 测试用脚本化模型证明的是「不画不置位 / 画则先置位 / 失败不写 / 不继承 / 只读不提供」等机制，不证明语言判断正确。
- PT 全量 pytest 本轮未重跑（PT 改动仅限 `/skills` 与 provenance 的取值白名单和迁移注释）。
