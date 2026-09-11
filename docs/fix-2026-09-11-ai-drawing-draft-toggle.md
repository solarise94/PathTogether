# fix-2026-09-11：「允许 AI 描绘」草稿期可开（创建参数链路）

## 问题

开关在草稿态被禁用（`integrations/pathtogether/ui/sessions.js` 约 621 行：
`noSession = !sid || !!S.aiDraft`），用户必须先发一条消息才能开，看上去像
功能未完成。根因：开关状态存在**服务端会话**上，草稿态还没有会话可写。
但工具列表在 run 启动时装配（`HistoPilot/src/tools.ts:2156`），草稿期提前
开对首轮 run 是完全可生效的——禁用是不必要的。

## 设计

把开关值作为**会话创建参数**随首轮请求带上去，在工具装配之前落为服务端
权威值。三层改动：

### 1. UI（HistoPilot/integrations/pathtogether/ui/）

- `sessions.js` `syncAiDrawingToggleUi()`：草稿态**允许**拨动；禁用条件只剩
  「run 进行中」。真实会话未知态仍显示关（反乐观回显原则不变）。
- 草稿选择持久化：扩展草稿暂存记录（`writeStoredDraft`/`readStoredDraft`
  那套 localStorage 机制，main.js 约 343/375-393 行），记录结构加
  `draw: boolean` 字段（缺省 false）。草稿态拨动 → 更新 `S.aiDrawingDraft`
  并写入暂存；`enterAiDraft` 恢复草稿时连开关一起回显。
- `main.js` `startAiRun()`：`POST /api/ai/run?fresh=1` 的 body 在草稿选择为
  开时带 `allow_ai_drawing: true`（严格布尔，只在为 true 时带该键）。
- `onSessionId`：拿到新 sid 后 `S.aiDrawingFlags[sid] = <草稿选择>`；权威回
  填仍由 `loadAndRenderTranscript` 的 GET detail 确认（现有机制不变）。
- i18n：`ai.drawing.need.session` 等提示按新禁用条件调整（草稿不再禁用，
  只剩 running 禁用）；检查 bridge-client.js 中 drawing 相关文案。

### 2. PathTogether（app.py）

- `api_ai_run`（约 16193 行）：body 接受严格布尔 `allow_ai_drawing`：
  - `fresh` 为真且值为 `true` → `payload["allow_ai_drawing"] = True` 透传；
  - 值为 true 但非 fresh（续写既有会话）→ 400 invalid_argument（改既有
    会话的开关必须走 `/api/ai/session/<sid>/drawing`，职责不混）；
  - 非布尔非缺省 → 400。
- 镜像落库：run 被接受（`_proxy_sse` 拿到 X-AI-Session-ID 触发
  on_accepted 的位置，约 16910 行）且本请求带了 `allow_ai_drawing=True`
  时，为新 sid 写 mirror 行——走 `share_store_pg.py:2313-2444` 现有的
  generation 预留/CAS 函数（初始 generation，参考 drawing 代理端点
  16584-16683 的写法），使 HP=true 与 PT mirror 一致，不产生
  mirror.stale 假警报。写 mirror 失败 → 记日志并照常返回（HP 侧已生效，
  用户可再拨一次修复 mirror；不得因此回滚已开始的 run）。
- 审计：`ai.run` 的 audit_detail 带上 `allow_ai_drawing`（true 时）。

### 3. HistoPilot（src/server.ts）

- `/run` 处理器：body 接受严格布尔 `allow_ai_drawing`；**仅在创建新会话
  （fresh）时**写入 session 持久化字段；非 fresh 忽略该键（既有会话的开关
  只认 `/drawing` 端点，与 generation 机制一致）。
- runner 注入（agent-runner.ts:2393 从 session 读）不变；默认关不变。

### 安全论证（为什么这不破坏 fail-closed）

- 绕过 PT 直连 sidecar 带 `allow_ai_drawing: true`：HP session 为 true 但
  PT mirror 无行 → 描绘写仍 403（平台闸门不变）。
- execute 期重读 session、只读 profile 拦截、branch 写入上限等纵深全部
  不动。
- 创建参数等价于「建会话后立刻拨开关」，只是消除了竞态。

## 不做

- 不做跨会话/跨切片的「记住我的选择」全局默认（草稿记录即边界）；
- 不改 system prompt；不改既有会话的开关端点语义；
- 不改 mirror 的 generation/CAS 机制本身。

## 测试与验收

- PT：`tests/test_ai_drawing_gate.py` 扩展——fresh 携带 flag 创建后 mirror
  为 true、伪造描绘写不再 403；fresh 不带 flag 行为回归（仍 403）；
  非 fresh 带 flag → 400；非布尔 → 400。
- HP：`test/ui-drawing-toggle.test.ts` 扩展——草稿态开关可拨、首发 body
  带 flag、onSessionId 后回显；server 侧 `/run` 接受字段的用例。
- 两个仓相关测试全绿；改动文件：HP 侧 sessions.js/main.js/bridge-client.js/
  server.ts + 测试；PT 侧 app.py + 测试。不动 tools.ts、不动
  share_store_pg.py 的 drawing 函数本身（只调用）。
