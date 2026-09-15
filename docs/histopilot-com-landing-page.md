# HistoPilot.com 介绍主页

日期：2026-09-14。状态：实施规格。

把未登录访问 `https://histopilot.com/` 看到的分流页升级成 ZCode 风格的产品介绍页。用户仍可从同一页进入 Demo 或登录。已登录用户继续进入完整应用，不经过介绍页。

## 1. 产品边界

- 路由不变：`AUTH_ENABLED=True` 且未登录时 `GET /` 渲染 `entry.html`；已登录或 `AUTH_ENABLED=False` 仍渲染完整 Viewer。
- Demo 仍是 `/demo`，登录仍是 `/login`。介绍页只做营销与分流，不加载 Viewer、OpenSeadragon 或 HistoPilot 插件。
- 不新建独立静态站、不改 DNS、不改 nginx。`histopilot.com` 已经指向同一套 PathTogether。
- 不使用真实病例截图。产品视觉用抽象 WSI + Agent 面板 CSS mock。
- 不引入 Google Fonts / CDN 字体。系统字体栈即可。
- 中英双语走现有 `static/i18n.js`（`data-i18n` + `.lang-toggle`）。
- 页脚必须保留研究/教学、不用于临床诊断的声明。测试会断言中文默认文案。

## 2. 参考风格（https://zcode.z.ai/en）

借鉴这些，而不是复制文案或 IDE 产品：

- 深色底 `#161616`，近白正文，低对比描边 `rgba(255,255,255,0.10)`。
- 粘性顶栏：左品牌，中导航锚点，右语言 + 登录白胶囊按钮。
- Hero 左文右产品 mock。大标题约 56–64px、字重 700、字距收紧。
- 主 CTA 白底黑字、大圆角；次 CTA 透明描边。
- 中部能力卡片：图标/插画 + 短标题 + 一句话。
- 页脚细分割线、版权、条款级链接。
- 大量留白，无卡片阴影堆叠，无苹果风浅灰 `#f5f5f7`。

不要照搬 ZCode 的下载区、定价卡、真实桌面截图。HistoPilot 的主行动是「进 Demo」和「登录」。

## 3. 页面大纲

### 3.1 顶栏

- 品牌：`HistoPilot`，链到 `#top`。
- 锚点：产品 `#product`、能力 `#capabilities`、套件 `#suite`。
- 外链：GitHub → `https://github.com/solarise94/HistoPilot`（新标签）。
- 语言按钮：沿用 `.lang-toggle`。
- 登录：`/login`，白胶囊，文案沿用 `entry.login` 的短版 `登录` / `Log in`（新增 `entry.nav.login`）。

### 3.2 Hero

- 小徽章：`研究软件 · 不用于临床诊断` / `Research software · Not for clinical diagnosis`
- H1：`让 Agent 自己读完整张切片。` / `Let an agent navigate the whole slide.`
- 副文：`HistoPilot 从低倍概览走到高倍确认，留下可复查的观察轨迹。PathTogether 提供协作 Viewer、项目、标注与分享。` / 对应英文。
- 主按钮：`直接体验 Demo` → `/demo`（**中文默认文案必须原样保留**，现有测试断言此字符串）。
- 次按钮：`登录测试与协作` → `/login`（中文默认文案必须原样保留）。
- 提示：`Demo 无需登录，可查看示例切片并体验 AI 导航`（中文默认文案必须原样保留）。

右侧 mock（纯 CSS，禁止真实切片图）：

- 外框像桌面应用窗口：顶栏交通灯、标题 `Demo slide · HistoPilot`。
- 左 62%：暗红/紫/粉的组织色块、脂肪空泡、比例尺、缩放控件。
- 右 38%：Agent 面板。固定 4 条轨迹文案（中英 i18n）：
  1. 低倍巡视腺体分布
  2. 放大到 20× 确认核分裂
  3. 在坐标 (12.4, 8.1) mm 留下观察
  4. 完成只读导航，未写入正式标注
- mock 只是装饰，不可点击进 Viewer。

### 3.3 产品如何工作 `#product`

三列步骤：

1. 打开切片 — 把 WSI 放进 PathTogether，或直接进入公开 Demo。
2. 让 HistoPilot 导航 — Agent 自己 zoom / snapshot / 标记观察，步骤有上限。
3. 协作与分享 — 登录后上传自己的切片、标注、评论，按权限分享。

### 3.4 能力 `#capabilities`

四张卡片：

1. Agentic 导航 — 低倍→高倍、坐标语义、快照、视觉预算。
2. 协作 Viewer — OpenSlide + OpenSeadragon，项目、ROI、评论。
3. 受控 Demo — 匿名只读、独立 capability、额度与只读工具集。
4. 插件宿主 — 版本化 Plugin Contract；HistoPilot 是独立服务，不进平台数据库。

### 3.5 套件 `#suite`

三列仓库卡，外链 GitHub：

| 名称 | 一句话 | 链接 |
|---|---|---|
| HistoPilot | 面向 WSI 的自主导航服务 | https://github.com/solarise94/HistoPilot |
| PathTogether | 协作读片平台与插件宿主 | https://github.com/solarise94/PathTogether |
| HistoPilot-DSH | 把导航注册为 DSH 高层工具 | https://github.com/solarise94/HistoPilot-DSH |

每张卡底部 `GitHub →`。

### 3.6 关闭 CTA

重复主/次按钮，标题：`先看 Demo，再决定是否登录。`

### 3.7 页脚

- 研究/教学声明必须包含中文默认句：`仅用于研究、教学和软件演示，不用于临床诊断。`
- 版权：`© 2026 HistoPilot`
- 链接：Demo、登录、三个 GitHub 仓库。
- 不编造尚不存在的 Terms / Privacy 路由。

## 4. 文案键

在 `static/i18n.js` 的 `zh` 与 `en` 都加。下列 **中文默认 HTML 文本** 不得改写，以免打测试：

| key | 中文（HTML 默认 / zh 字典） | 英文 |
|---|---|---|
| `entry.demo` | 直接体验 Demo | Try the Demo |
| `entry.login` | 登录测试与协作 | Log in for testing & collaboration |
| `entry.demo.hint` | Demo 无需登录，可查看示例切片并体验 AI 导航 | The Demo needs no login: view sample slides and try AI navigation |
| `entry.footer` | 仅用于研究、教学和软件演示，不用于临床诊断。 | For research, teaching, and software demonstration only — not for clinical diagnosis. |
| `entry.tagline` | 可改成更短的 hero 副文；若改 HTML 默认，测试未锁此句 | 见 §3.2 |

新增键（名称可微调，但语义固定）：

```
entry.nav.product
entry.nav.capabilities
entry.nav.suite
entry.nav.github
entry.nav.login          # 短：登录 / Log in
entry.badge
entry.hero.title
entry.hero.lead
entry.mock.title
entry.mock.step1
entry.mock.step2
entry.mock.step3
entry.mock.step4
entry.how.kicker
entry.how.title
entry.how.s1.title / .body
entry.how.s2.title / .body
entry.how.s3.title / .body
entry.cap.kicker
entry.cap.title
entry.cap.1.title / .body
entry.cap.2.title / .body
entry.cap.3.title / .body
entry.cap.4.title / .body
entry.suite.kicker
entry.suite.title
entry.suite.hp.body
entry.suite.pt.body
entry.suite.dsh.body
entry.suite.github
entry.cta.title
entry.cta.body
entry.footer.copy
```

语言切换后 mock 轨迹、卡片、导航必须一起切换。`document.title` 可在 `hp-lang-change` 或 `applyLang` 后设为 `HistoPilot` / 带短副标题；不要依赖未实现的 `data-i18n-document-title`。

## 5. 实现约束

文件：

- 重写 `templates/entry.html`。结构：`header` + `main`（hero / how / capabilities / suite / cta）+ `footer`。
- 新文件 `static/entry.css`。`entry.html` 用 cache-bust 引用，例如 `?v=20260914a`。
- `static/i18n.js` 补键；`entry.html` 的 i18n 脚本 query 同步升级，避免旧缓存。
- `tests/test_phase1_auth_ui.py` 现有断言必须继续通过，并补：
  - 未登录 `/` 含 `entry.css`、`#capabilities`、三个 GitHub 链接；
  - 仍不含 `id="viewer"`；
  - 已登录 `/` 与 `AUTH_ENABLED=False` 行为不变。

技术细节：

- 语义 HTML、可键盘到达的按钮/链接、对比度足够（白字深底）。
- 响应式：≥1080px 为 hero 左右分栏；平板 mock 到标题下；手机顶栏导航可改成锚点横滑或折叠，但 CTA 必须首屏可见。
- 减少动画。若用渐变/浮动，尊重 `prefers-reduced-motion`。
- 不加载 `style.css`、`app.js`、`openseadragon`。
- 空 favicon 可用 `data:,`，与登录页一样避免未登录撞 `/favicon.ico` 鉴权。
- 内联 SVG 图标可以；不要外链图片。
- 组织色 mock 用 H&E 感觉的抽象色（嗜伊红粉、苏木素紫），不要写具体病名。

## 6. 验收

1. `pytest tests/test_phase1_auth_ui.py -q` 通过。
2. 未登录 HTML 含 `直接体验 Demo`、`href="/demo"`、`href="/login"`、临床诊断声明。
3. 桌面：深色、粘性顶栏、大标题、右侧 mock、主白 CTA。
4. 中英切换覆盖导航、hero、卡片、页脚。
5. Demo / 登录链接可点，mock 不导航到 Viewer。
6. 已登录 `/` 仍是完整应用。
7. 无真实切片、无编造的隐私政策路由、无外部字体。
