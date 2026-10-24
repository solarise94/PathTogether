# 转换器 Mac 保存提示修改说明

日期：2026-10-22。供 Opus 实现；本文仅为修改说明，尚未修改产品代码或发布。

## 1. 用户确认的现象与本次决定

用户在 Mac 上保存转换结果时遇到卡顿，观察到系统进程 `ImageThumbnailExtension`，等待数分钟后恢复。用户实测，选择默认“下载”（Downloads）文件夹以外的位置保存，可以缓解。

本次只增加保存提示，保留现有转换格式、文件后缀、OPFS 存储和保存流程。不实施此前讨论的 `.hpslide`、SVS、OME-Zarr 或 ZIP 导出，也不要求用户关闭系统索引或预览。

上述现象来自用户反馈，尚未完成 Mac 对照实验。文案用“可能”“可尝试”，不承诺换目录必然有效，不宣称已证明所有卡顿都由系统缩略图造成，也不把它描述为转换器保存失败。

## 2. 目标页面与代码位置

目标页面：`/tools/slides`。

当前核对的工作目录：

```text
/home/solarise/ZCodeProject/histopilot-suite/worktrees/release-20261021
```

该目录已有大量其他任务的未提交改动，包括下列目标文件。实现前重新核对当前工作树及发布基线，在既有内容上做增量修改，不覆盖、回退或整体格式化其他任务的工作。

| 文件 | 本次用途 |
| --- | --- |
| `templates/tools_slides.html` | 结果区及历史任务区的保存提示 |
| `static/i18n.js` | 中英文文案 |
| `static/tools/tools-slides.css` | 优先复用既有 `.hint` 样式；仅在排版确有需要时调整 |

参考位置：结果区 `#result-section`，保存按钮 `#save-btn`；历史任务区 `#jobs-details`，任务列表 `#jobs-list`。

保存逻辑位于 `static/tools/tools-slides.js` 的 `onSave(job = null)`；文件类型位于 `static/tools/slide-transform/engine.js` 的 `saveFileTypes()`。本次无需修改这两处逻辑。

## 3. 建议采用的文案

新增中英文键：`tools.result.save.mac.hint`。

中文：

> Mac 用户提示：保存大型 TIFF 切片后，系统缩略图进程 ImageThumbnailExtension 可能短时占用较多内存和 CPU，造成数分钟卡顿。可尝试保存到默认“下载”（Downloads）文件夹以外的位置，以缓解此现象。

英文：

> Mac users: After saving a large TIFF slide, the macOS thumbnail process ImageThumbnailExtension may temporarily use substantial memory and CPU, causing slowdowns for several minutes. Saving to a folder outside the default Downloads folder may help.

模板的中文兜底文本与词典保持一致。切换语言时使用已有 `data-i18n` 机制更新。

## 4. 展示要求

采用静态说明文字，明确以“Mac 用户提示”开头，对所有平台可见。这样无需新增平台探测逻辑，也便于其他设备上的用户提前了解 Mac 使用注意事项。

1. 在结果区的“保存到电脑 / 申请持久存储”按钮组之前添加提示，复用 `.hint`，让用户在点击保存之前看到。
2. 历史任务列表也能直接保存产物，且此时结果区可能隐藏。因此在 `#jobs-details` 内、`#jobs-list` 前复用同一提示键，展开历史任务即可看到。无需为每个任务重复渲染。
3. 提示是普通说明段落，不使用弹窗、额外确认、`role="alert"` 或反复播报的动态状态。
4. 完整显示进程名 `ImageThumbnailExtension`，便于用户在活动监视器中辨认。窄屏允许换行，不产生横向滚动。
5. 不主动替用户选择其他目录，不自动创建文件夹，不改变浏览器保存目录偏好。目录仍由用户在现有保存对话框中选择。

两个提示节点复用一个翻译键即可，无需增加页面脚本或新依赖。

## 5. 必须保持的行为

- 本地转换、校验、保存全程的网络与隐私语义保持不变。
- 继续导出实际的 TIFF / OME-TIFF，文件命名、MIME 类型、分块写出与输出字节保持不变。
- 原有“正在写出”“保存成功”“保存取消”“保存失败”的状态判断保持不变；静态提示不能覆盖它们。
- 原有 OPFS 临时存储提示保留，Mac 提示不能替代它。
- 不从用户反馈推导固定恢复时长，不显示自动倒计时，也不保证数分钟后一定恢复。

## 6. 验收与交付

- 新转换完成后，点击保存前能看到 Mac 提示。
- 刷新后，展开历史任务，即使结果区隐藏，也能在历史保存入口附近看到提示。
- 中英文切换正常，无翻译键原文露出；模板中文兜底可读。
- 桌面和约 390px 窄屏下无溢出，按钮与既有状态信息正常显示。
- 原有保存流程无需额外确认；取消或失败仍保留浏览器内产物。
- 不为纯文案改动新增镜像实现的测试。按最终改动选择必要的既有页面检查，可参考 `tests/js/tools-u2-page.test.ts` 与 `tests/js/tools-page-usability.test.ts`；优先检查真实展示效果。
- 所有本机测试遵守工作区 `AGENTS.md`：先确认 SSD2 挂载与可写，运行数据放在 `/mnt/ssd2/tests` 的独立私有目录，启动 Python、Node、浏览器或测试服务器前设置 `TMPDIR`、`TMP`、`TEMP`，相关缓存、截图与报告也显式指向该运行目录。交付时记录实际目录和检查结果。
- Mac 实机若未复测，应明确说明“提示基于用户反馈，本次未独立复现”，不要将页面文案检查写成 Mac 故障已修复。
- 交付说明列出实际修改文件和验证结果，区分本地完成与已上线。本文不要求自动部署。
