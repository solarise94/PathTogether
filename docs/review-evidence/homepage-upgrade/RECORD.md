# 主页升级（H1-media）：工作台预览截图来源记录

按 `docs/homepage-onboarding-upgrade-agent-plan-20260928.md` §5「优先：真实界面截图」
在本地隔离环境拍摄。本文件记录取景来源、代码版本、视口、示例数据来源与可复跑
命令；测试截图归档在 `artifacts/`，发布用资源在 `static/entry-media/`。

## 取景来源

- 软件：本仓库（PathTogether）`/app` 工作台，普通角色用户（role=user，
  显示名「预览用户」）视角；非 admin 控制台，无未发布功能。
- 画面内容：左侧「未归类切片」列表 +「导入切片」上传入口；中央 OpenSeadragon
  查看区渲染的合成示例切片（真实 openslide 1.4.6 瓦片解码）；查看区内一条
  名称「观察标记」的箭头标注（UI 真实绘制并经 POST /api/annotation 保存）；
  右侧「标注」面板列出该标注。三处关键区域齐备（计划 §5 要求）。
- 拍摄流程走真实 UI：登录表单 → 展开侧栏 → 打开「示例切片 A.tif」→
  标注选项 popover 填名称 → 箭头工具在切片上拖拽 → 打开标注面板 →
  「显示全部标记」👁 开启 → 截图。
- 不含真实邮箱 / 患者 / 研究者资料 / token / 私有切片名；示例数据全部为本
  次合成（见下）。截图为展示材料，不承诺图中按钮可点击（页面另有文字标注）。

## 示例数据来源

- 两张合成切片（`示例切片 A.tif` / `示例切片 B.tif`，各 4096×4096 单层
  tiled TIFF，deflate）：`boot_preview_app.py:synth_slide_tif` 用 numpy
  确定性伪随机生成伪 H&E 图案（浅粉间质 + 品红腺体环 + 紫色核点），
  无任何真实组织/患者数据；经 `slide_publish.publish_standalone` 离线发布
  为 ready。切片无物理标尺（mpp missing），页面如实显示「无物理标尺」。
- 账号：`preview@pt.test`（普通用户）与 `preview-owner@pt.test`（bootstrap
  owner），随机密码，仅存于临时凭据文件 `/tmp/pt-preview-creds-<port>.json`
  （0600），进程退出即随临时目录销毁。

## 代码版本

- 工作树（slide-id-refactor 分支，主页升级 H1/H2 改动已写入，未提交哈希以
  `git rev-parse HEAD` 当次输出为准；上一提交 acb1e8e）。
- 拍摄时点的 entry.html / app.js / _app_shell.html 为当前工作树版本。

## 视口与产出

| 文件 | 视口 | 说明 |
|---|---|---|
| `artifacts/workbench.png` | 1440×900（≥1440 工具栏全展开，标注组不被折叠进 ⋯） | 工作台原图（证据归档） |
| `static/entry-media/workbench-preview.webp` | 1440×900，q80 | 桌面首选，81KB（预算 ≤250KB） |
| `static/entry-media/workbench-preview-720.webp` | 720×450，q78 | ≤767px 手机用，23KB（预算 ≤120KB） |
| `static/entry-media/workbench-preview.png` | 1440×900，256 色量化 | 无 WebP 回退，211KB |
| `artifacts/entry-after.png` | 1440×900 | 升级后主页（未登录，中文） |
| `artifacts/entry-after-en.png` | 1440×900 | 升级后主页（英文） |
| `artifacts/entry-before.png` | 1440×900 | 升级前主页（未登录，中文；见下方「前截图来源」） |

发布资源由 `artifacts/workbench.png` 一次性生成（PIL），非另行拍摄。

## 前截图（entry-before.png）来源

升级前页面来自 `git worktree add /tmp/pt-before-head HEAD --detach`（acb1e8e，
主页改动全部未提交时 HEAD 即升级前状态），把本目录 `boot_preview_app.py`
复制进 worktree 后以 `--port 8920` 起同一隔离环境（注意避开主预览应用的
share 端口 8918=8917+1），1440×900 未登录截图后 `git worktree remove` 清理。

## 可复跑命令

```bash
# 1) 起隔离被测环境（端口 8917；等待输出 PREVIEW_READY）
python3 docs/review-evidence/homepage-upgrade/boot_preview_app.py --port 8917 &

# 2) 登录工作台画标注 + 截图（工作台 1440×900 / 主页 1440×900 中英）
node docs/review-evidence/homepage-upgrade/shot_workbench.mjs --port 8917

# 3) 生成发布资源（webp/png 三档）
python3 - <<'PY'
from PIL import Image
src = Image.open('docs/review-evidence/homepage-upgrade/artifacts/workbench.png').convert('RGB')
src.save('static/entry-media/workbench-preview.webp', 'WEBP', quality=80, method=6)
src.resize((720, 450), Image.LANCZOS).save('static/entry-media/workbench-preview-720.webp', 'WEBP', quality=78, method=6)
src.quantize(colors=256, method=Image.FASTOCTREE, dither=Image.FLOYDSTEINBERG).save('static/entry-media/workbench-preview.png', 'PNG', optimize=True)
PY
```

拍摄后清理：`kill $(cat /tmp/preview-app.pid)`（pg_reap 收割临时 PG 与数据目录）。

## 复现注意（拍摄时踩到的真实行为，非脚本缺陷）

- 工具栏宽度分档（app.js `TB_FOLD_SPECS`）：1024–1439px 时「标注」组折叠
  进 ⋯ 菜单，`#anno-arrow-btn`/`#anno-btn` 不可点；≥1440px 才全展开。
- 新打开切片时 `state.showAnno=false`：已保存标注默认不画在画布层，需点
  「显示全部标记」👁（`#anno-all-toggle`）才渲染。
- 未命名标注的默认标签是固定文案「管理员」（i18n `anno.default.user`，
  与账号角色无关）；为避免误导，拍摄时通过真实 UI 填写名称「观察标记」。
- 脚本内建验证：截图前统计 `#anno-canvas` 非透明像素（本次 2451），
  为 0 即失败，防止「面板开了但标注没画」的静默坏图。
