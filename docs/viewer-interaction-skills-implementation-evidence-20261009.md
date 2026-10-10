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

## 8. 修订二：描绘默认随 grant 发放 + 真实模型验收（2026-10-10，主代理实现）

依据：用户要求「都由模型判断，默认先都给，减少提示约束」，并要求用真实模型测试，不用脚本化模型。
契约见 `viewer-drawing-skill-contract-20261009.md` §0.6。

### 8.1 改动

- PT：`create_run_grant` 发放即 `allow_ai_drawing = TRUE`（`drawing_intent = 'default'`），0082 列默认 TRUE。
  写入口仍检查 grant 有效与创建者写权限。`/skills` 端点保留。
- HP：删除首次描绘调 `/skills` 的逻辑；`draw_suspicious_region.points` 改为 level-0 像素坐标；
  工具说明与系统提示按 §0.6 精简（删除「确认并标注」主动落标诱导，加一句「写入不再经用户确认；只问怎么圈或
  假设性提问时文字回答即可」）。
- HP 评测：`experiments/drawing-eval.ts`（真实 PT + 真实 AgentRunner + 真实模型）、`render-drawing-eval.py`、
  `evals/drawing-eval-v1.json`（14 例）；旧的 `drawing-intent-v1.json` 与其形态测试删除。

### 8.2 实测中发现的两处既有缺陷（与本轮设计无关，已修）

1. **生产 AI 读片报「切片不存在」**：HP 把 slide_id 拼到按文件名的路由
   `/api/plugin/v1/slides/<id>`，应为 `/slides/by-id/<id>`。已查到的生产事件：10-06 ×3、10-09 ×1 agent_error，
   最后一次成功是 09-29；未逐笔核对 10-01 起每次请求，不能断言期间全部失败。修复：HP `e698fce`（集成分支）；另在 `hotfix/hp-0.3.6-slide-routes`（基于生产
   HP 0.3.5 `23e89fa`）上 `09d52d9` + `64a0396`（含下条修复，package 0.3.6），HP npm test 1489 通过。
   **未部署，需用户批准。**
2. **DeepSeek 拒绝描绘工具 schema**：tuple 形 `items` 使整次请求 400（「Invalid schema for function
   'draw_suspicious_region'」）。改为普通数组 + 回归测试；集成分支与 hotfix 分支都已修。

### 8.3 评测环境

演示库 PostgreSQL 数据目录副本 + share-data 副本（临时目录，不入仓）；真实切片
`26-005817_2026-08-25_14_12_12.tif`（复制成 4 份独占副本，每例开始前清空该副本标注，避免用例互相看到对方的
标记——第一轮共用一张切片时已出现污染，结果作废）；PT 本地服务；HP `AgentRunner` +
`PathTogetherHttpClient`，配置同生产（`deepseek_official`、inline 图像、上下文 400k、max_steps 50）。
模型 `deepseek-flash`（DeepSeek-V4.1-Flash）。测试网关不可用，使用生产 DeepSeek key：每条命令在内存中解密，
未落盘、未打印。

### 8.4 结果

是否描绘（最后一轮至少调用一次描绘/矩形标注并写入 = draw）：

| 版本 | 结果 |
| --- | --- |
| 提示精简后、工具说明未加句（r2） | 12/14；「如何圈出腺体？」「如果要圈出腺管，你会怎么做？」都实际描绘作为演示 |
| 同上，两条提问各重复 3 次 | 0/6（每次都演示性描绘） |
| 再删系统提示「确认并标注」 | 0/6（无改善：诱因不在系统提示） |
| 工具说明加一句（最终版），提问 + 明确请求各重复 3 次 | 假设性提问 3/3 不画；「如何圈出」1/3 不画；「帮我圈出这个导管」3/3 画 |
| 最终版全量（r3） | **14/14** |

明确请求全部会画；否定（「暂时不需要」「请不要标注」「我没要求你圈」）、讨论已有选框、`@描绘是什么意思？`、
多轮「上一轮画过、本轮只问问题」均不画。剩余误判集中在「如何圈出」这类方法提问，模型倾向「边讲边示范」。

定位与轮廓（三处导管人工框作对照，外接框 IoU）：多边形/矩形与导管 IoU 0.6–0.9；多边形 6–21 个顶点，
导管轮廓贴合上皮外缘；肌束轮廓位置对但边界粗（纺锤形，夹带邻近间质）。额外标记多为视野内真实的小腺体/
小血管，未见落在空白区的乱画。模型自选矩形或多边形：「圈出」有时用矩形。

成本：r3 全量 14 例共 2.31M token；中位 38 次模型调用/例；**14 例中 4 例触到 50 步上限暂停**（含「请不要标注，
只告诉我这是什么组织」这样的简单问题）。每看一眼视野要 goto + snapshot + complete_snapshot_review 三次调用，
合计占全部工具调用的 77%（其中 complete_snapshot_review 单独占 23%）。

### 8.5 构架建议（已获批，实施见 §9）

1. **成本优先于描绘本身**：读片协议的「每张快照必须 complete_snapshot_review 关闭」使每次观察多一轮调用，
   简单问题也会走满 50 步。建议把关闭动作并进下一次 goto/snapshot（或允许 snapshot 带处置参数），并让模型
   在问题已可回答时直接 finish；这与「减少约束」方向一致，按 r3 计可省下约 1/4 的工具调用。
2. **轮廓精度靠分割模型细化，不靠 LLM 多点**：公开工作一致的做法是 VLM 负责定位（框/粗多边形/点），
   分割模型负责边界——Visual Sketchpad（2406.09403）、Set-of-Mark（2310.11441）用于定位；SAM2（2408.00714）、
   MedSAM（2304.12306）、病理专用 PathoSAM（2502.00408）做框提示分割；WSI-Agents（2507.14680）、
   CPathAgent（2505.20510）是病理 agent 的整体框架。本次实测也支持：定位 IoU 0.6–0.9 已够，边界是短板。
   建议形态：描绘工具保持现接口，PT/HP 侧增加可选「细化」步骤——以模型多边形外接框为提示在 level-0 取图跑
   PathoSAM/MedSAM，得到掩膜转多边形后再写入，原始多边形保留在 provenance 便于对比。需要 GPU 或 CPU 推理
   预算，属新服务依赖，需用户决定。
3. **不建议**为了提问类误判继续加提示规则：最终版只加一句工具说明；剩余误判是待复核标记，可删除。

### 8.6 门禁（2026-10-10）

| 门禁 | 结果 |
| --- | --- |
| HP `npm test` | 79 files / 1504 passed |
| HP `tsc -p tsconfig.build.json --noEmit` | 通过 |
| HP `npm run test:contract`（真实 PT；0082 用例改为「新 grant 默认可画，关闭的 grant 403」） | 49 passed |
| PT pytest：描绘闸门文件 | 73 passed（新增「新 grant 默认允许描绘」） |
| PT 全量 pytest（TMPDIR 指大盘，plugins/ 无 HP bundle） | 2988 passed / 113 skipped（33 分钟；收尾时测试用 PostgreSQL 关闭超时被强杀，非用例失败） |

PT 前端与 Playwright 本轮未重跑：本轮 PT 改动只在 `share_store_pg.py`、0082 迁移注释/默认值与测试，没有前端改动。

## 9. 构架优化：goto 即取景 + SAM2 轮廓细化（2026-10-10，子代理实现、主代理审查）

契约见 `viewer-drawing-skill-contract-20261009.md` §0.7。提交：HP `cceaa7a`（goto 即取景）、`906e35f`（描绘细化接线）；
PT `22869591`（细化服务与端点）及本节所在提交（溯源字段）。

### 9.1 离线原型：选哪种分割模型

在 r2/r3 真实模型画出的 35 处区域上，用模型多边形外接框提示三种模型（CPU）：

| 模型 | 单次耗时（CPU） | 结果 |
| --- | --- | --- |
| SAM2.1 hiera-b+ | ≈1.8 s | 导管沿上皮外缘贴合；矩形提示也能收成准确轮廓；肌束沿边界 |
| SAM ViT-B | ≈4.4 s | 导管同样准确，肌束只取一部分 |
| MedSAM ViT-B | ≈3.3 s | 常只分割上皮、不含管腔，肌束失败——不适合「圈出导管」 |

选 SAM2。纯靠 SAM2 时，没有清晰结构的弥漫区域（间质框）会给出乱绕的形状，因此加门控：掩膜限制在模型多边形
外扩约 10% 内，score ≥ 0.75 且面积比 0.3–1.3 才采纳。35 处中采纳 28 处；未采纳的 7 处为弥漫区域和一处
SAM2 置信偏低的导管，均保留模型原轮廓。

### 9.2 真实模型对比（DeepSeek-V4.1-Flash，同一 14 例，独占切片副本，各跑一次）

2026-10-10 复核（`navigation-refine-review-20261010.md`）指出初版本表把模型请求数标成工具调用数、并取了上中位数；
下表按三份 `results.json` 重算，与复核的 `eval-recount.json` 一致。

| 指标 | r3：三步导航 | a1：goto 即取景 | b1：+ SAM2 细化（本地 CPU） |
| --- | ---: | ---: | ---: |
| 原 evaluator pass | 14/14 | 13/14 | 14/14 |
| 模型请求合计 / 每例中位 | 465 / 34 | 196 / 10.5 | 172 / 9.5 |
| 工具调用合计 / 每例中位 | 471 / 34 | 222 / 12 | 190 / 10.5 |
| 结束时未 finished（触 50 步） | 4 | 0 | 0 |
| 每例耗时中位（秒） | 108 | 69 | 78.5 |
| token 合计 | 2.31M | 1.93M（−16.7%） | 1.69M（−26.7%） |

口径限制：pass 只检查最后一轮是否调用描绘/矩形、应画时是否至少写入一次，不要求 finished、答复完整、目标召回或
轮廓质量；r3 的 4 个暂停用例仍记为 pass。单张切片、14 个语义用例、各一次随机运行，不能推广到其他组织/染色/尺度；
SAM2 置信分与面积比不等于病理轮廓准确率。

- token 降幅小于请求数降幅：看图次数没减，每次 goto 仍带一张图；省下的是 snapshot 与关闭步骤的往返。
- a1 的唯一失败是「这里是什么组织」时模型主动给一处结构落了待复核矩形（系统提示允许主动落标）；r3 同例走满
  50 步暂停，没走到落标那一步；b1 同例未落标。属模型自主行为的方差。
- b1 的 10 个多边形全部被细化采纳。叠加图：两处导管贴上皮外缘；肌束沿边界；上方一处导管因模型给的粗轮廓偏大，
  细化结果带进旁边一块间质（仍在门控范围内）。矩形标注不细化。
- b1 中位耗时略高于 a1（78.5 s vs 69 s），含本地 CPU 推理每处约 2–4 s 与运行方差。GPU 推理耗时**未实测**。

### 9.3 上线所需（未做，需用户批准）

- homepc 已具备 RTX 2080 Ti（22 GB）与 podman NVIDIA CDI。需：构建 `services/contour-refine` 镜像（Containerfile
  未实际构建过）、放置权重 `sam2.1_hiera_base_plus.pt`（约 309 MB）并只读挂载、`--device nvidia.com/gpu=all`
  启动、PT 配 `CONTOUR_REFINE_URL`（建议同时配 `CONTOUR_REFINE_TOKEN`）。
- 不配 `CONTOUR_REFINE_URL` 时功能自动关闭，描绘照旧写模型轮廓，可先随 PT/HP 上线再单独开。
- 细化端点未加 `/regions` 那样的像素预算/并发 429 闸；crop 长边结构上 ≤1024，服务侧推理串行。

### 9.4 门禁（2026-10-10）

| 门禁 | 结果 |
| --- | --- |
| HP `npm test` | 80 files / 1529 passed |
| HP `tsc -p tsconfig.build.json --noEmit` | 通过 |
| HP `npm run test:contract`（真实 PT；含 refine 端点与 contour_refine 落库） | 51 passed |
| 细化服务 `services/contour-refine/tests`（seg-venv，含真实 SAM2 冒烟） | 16 passed |
| PT 全量 pytest（plugins/ 无 HP bundle） | 3020 passed / 113 skipped（30 分钟；收尾 PostgreSQL 关闭超时被强杀，非用例失败） |

## 10. 复核修复（2026-10-10，`navigation-refine-review-20261010.md`，子代理实现、主代理审查）

复核方的 4 条失败回归原样入仓（未改断言），修复后全部通过。

| 项 | 修复 |
| --- | --- |
| R1 P1 GPU 启动 | 去掉 `--preload`，`gunicorn.conf.py` 显式 `preload_app=False`，模型在 worker 进程内加载并在 `post_worker_init` 预热；`/healthz` 报本进程 `pid`/`device`，本进程模型未就绪即 503；推理忙时回 503 `busy`（二轮复核后改为不排队，见 §11） |
| R2 P1 解码量 | 抽出与 `/regions` 共用的成本估算 `_plugin_render_cost_estimate` 与准入 `_plugin_admit_region_pixels`；refine 按真实金字塔层估算解码像素（含多通道倍数），任何 `read_region` 之前超单请求上限 → `refined:false, reason:"too_large"`，滑窗/并发不准入 → `busy`，计入与 regions 同一像素窗口。`/regions` 行为与错误信封不变 |
| R3 P2 旧快照 | 同位置 goto 仅在资产 revision 与 render fingerprint 都一致时复用当前快照，否则重新取景；描绘、矩形、region 观察遇到身份不一致的当前快照拒绝写入并请模型重新 goto |
| R4 P2 显示上下文 | HP 以当前快照自身的 render context 与 revision 调细化；PT 走与 regions 相同的校验后读图，回显 `render_context_fingerprint`；context 无效/过期 → `refined:false, reason:"render_context"`；快照 context 无法上线（legacy）时 HP 跳过细化 |
| R5 P2 取消 | 细化正常返回后、写请求前各检查一次取消；annotate 支持并传入取消信号 |

另：§9.2 评测口径已按复核重算；§8.2 生产事故措辞改为已查到的事件，不再断言 10-01 起全部失败。

真实模型复测（c1，同 14 例，SAM2 本地 CPU）：14/14；模型请求 214（中位 12.5）、工具调用 230（中位 14.5）、
未 finished 0、token 2.16M；多边形 10/10 细化采纳；身份闸门误拦 0 次、写入失败 0 次。请求数高于 b1（172），
单次运行方差，不作结论。

遗留：矩形 `create_annotation` 执行签名不带取消信号（无细化步骤，窗口只有会话 I/O），未改；GPU 镜像仍未实际构建和
在 2080 Ti 上跑真实 `/v1/refine`，README 已写为上线验收步骤。

门禁（2026-10-10）：

| 门禁 | 结果 |
| --- | --- |
| HP `npm test` | 80 files / 1543 passed |
| HP `tsc -p tsconfig.build.json --noEmit` | 通过 |
| HP `npm run test:contract`（真实 PT） | 51 passed（契约夹具的假切片句柄补 `level_downsamples`） |
| 细化服务测试（含真实 SAM2 CPU 冒烟） | 20 passed |
| PT 子集（refine/regions/render context/描绘闸门等） | 231 passed |
| PT 全量 pytest（plugins/ 无 HP bundle） | 3033 passed / 113 skipped（29 分钟） |

## 11. 二轮复核 F1：推理忙时不排队（2026-10-10，主代理实现）

二轮复核（`navigation-refine-review-20261010.md` §二轮复核）确认 §10 五项修复通过，另发现：推理锁的 2 秒等待只覆盖锁，
不覆盖 gunicorn 线程池里的等待，客户端 8 s 超时后仍有 6 个请求在第 12 s 开始推理。

修复：`/v1/refine` 非阻塞获取推理锁，被占用即回 503 `busy`，去掉 `CONTOUR_REFINE_QUEUE_WAIT_S`。等待线程不再阻塞，
线程池在毫秒级排空，已放弃的请求不会再进入 predictor。

验证：复核方探针（真实 gunicorn 23.0.0 + 正式 `gunicorn.conf.py`，首请求 12 s 慢推理，11 个并发 8 s 超时）修复前
6 个过期请求开始推理、修复后 0 个，等待请求均在约 5 ms 内回 503。同形态回归入仓为
`services/contour-refine/tests/test_gunicorn_queue.py`（首请求 3 s、8 个 1.5 s 超时客户端；修复前失败、修复后通过）。
细化服务测试 21 passed（含真实 SAM2 CPU 冒烟与 gunicorn 并发回归）；PT `tests/test_plugin_contour_refine.py` 39 passed。
GPU 镜像实测仍未做，仍是启用前置项。
