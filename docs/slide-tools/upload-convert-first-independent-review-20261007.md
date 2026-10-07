# 先转换后上传：独立审查与 dogfood（2026-10-07）

审查提交：`5fa9b55`，分支 `upload-convert-first`。基线为已上线的 `ux-formats`；本轮没有修改产品代码、推送或部署，也没有写入生产数据库或 COS。

**结论：暂不建议发布。六种新输入的正常转换路径通过本轮页面测试，但新增直传控制器和共享嗅探存在可复现的问题，详见下文。**

## 1. [P1] 直传续传只认文件名和大小，可混合两份文件

位置：`static/tools/tools-slides-direct-upload.js:56–58,147–156,181–184`。

`resumableFor` 只比对 filename/size，无内容身份；上传时 `resumeJobId` 自动复用、`skipConfirm=true`。共享上传器按记录跳过已经确认的分片；创建请求只带 direct_class，没有整文件期望哈希。ListParts 能核实云端分片存在，不能证明重新选择的本地文件与之前相同。

**实测**：构造两份同名同大小、可正常解码的 64×64 RGB TIFF（像素分别为 32、192），模拟旧任务分片 1 已确认，再选择新文件。真实页面和上传器只 PUT 分片 2。按上传内容与旧云端分片重建的文件既不等于旧文件也不等于新文件，仍可解码为 64×64×3：5,984 个通道值为 32，6,304 个为 192。上传后端在此实验中为有状态替身，未声称真实云端发布了混合文件；浏览器错误复用与混合字节均已实证。

建议：续传前验证有界内存计算的完整内容身份，并绑定账号；无法验证时拒绝复用或重传全部分片。不能依赖名称/大小或 ListParts。增加同名同大小异内容、账号变化、刷新后重新选文件的回归。

## 2. [P1] 普通 TIFF 嗅探不按 IFD 偏移读取，连本工具导出文件也会拒绝

位置：`static/upload/slide-sniff.js:575–615`。

只有 BIF 支持按首 IFD 指针跳读；其他 TIFF 只检查前两个 128 KiB 窗口。IFD 或描述在文件后部时，classifyFile 返回 temporary/legacy-direct。转换页接着送入转换探测，核心正确识别为 OME-TIFF 后拒绝“再次转换”，但页面没有显示直传面板。

**浏览器实测**：选取现有转换器导出的 `bf-580x300-native.tif`，`direct-section` 不可见，页面报：

> OME-TIFF 不是转换输入：平台可直接读取 OME-TIFF，请直接上传该文件

BIF 转出的 OME 也误判。真实公开 CMU-1.ndpi 与 Leica-1.scn 的共享嗅探结果均为 temporary、compression=0，导致工作台绕过本轮转换分流继续直传；OS-2.bif 则能判为 convert。

建议：所有 TIFF 类型都按魔数、首 IFD 偏移、必要标签偏移做小范围随机读取，限制单次及总读取预算；不要用“只读文件开头”代替有界读取。回归必须覆盖真实转换器输出，不只用 IFD/OME-XML 位于文件头的简化夹具。

## 3. [P2] 新直传面板完成后仍可重复上传

位置：`static/tools/tools-slides.js:381–390`；`static/tools/tools-slides-direct-upload.js:173–175,200–218`。

成功时删除上传记录，finally 无条件重启按钮，控制器只防并发、不记完成。用户看到“已发布”后再次点击，会新建 ingestion 并重传。

**浏览器实测**：第一次点击创建数 1；等待发布完成，再点击同一按钮，创建数变成 2。这与原转换产物上传路径曾修复的重复发布问题相同。

建议：保留 published receipt，成功后展示打开切片入口；项目关联待完成时只重试关联，不能重新传对象。覆盖重复点击、刷新再选同文件及跨标签页。

## 4. [P2] 直传项目关联失败仍回调成功，且没有可恢复记录

位置：`static/tools/tools-slides-direct-upload.js:173–175,200–211`。

共享上传器成功即清除记录；随后 associateTarget 失败仅改状态文本，仍调用 onPublished 并返回 ok=true。项目目标、已发布 slideId、待关联状态均未持久化，没有仅重试关联的路径。

**实测**：真实浏览器中导入该模块，调用实际控制器，注入项目关联接口 503。返回 `ok=true, assocError=true`，成功回调调用一次，localStorage 上传记录为空。此项针对控制器目标分支的故障注入，不是一次真实 COS 上传。

建议：持久化 published + association_pending，关联成功后才向工作台发送完整成功；刷新和重试沿用同一个 slideId、项目幂等键。

## 5. [P2] BigTIFF 大端首 IFD 偏移解析错误，回归夹具也写错了

位置：`static/upload/slide-sniff.js:104`；`tests/js/slide-sniff.test.ts:286–287`。

首 IFD u64 无论字节序都按“低 32 位在前”拼接。合法大端文件首 IFD=16（字节为 `00 00 00 00 00 00 00 10`）会读成 16×2^32。现有所谓大端测试在头部也按低字在前构造，所以测试通过不能说明大端有效。

**实测**：独立用 DataView.setBigUint64(..., false) 构造合法头，相同 OME 描述的小端返回 ome-tiff，大端返回 temporary。

建议：使用 getBigUint64(offset, little) 并验证安全整数/文件边界；测试夹具必须按规范写入 u64，不复制被测代码的错误拼接方法。经典 TIFF 条目数另有 `getUint16(0,true)` 硬编码，也应一并审查。

## 6. 交互缺口：新直传面板没有字节进度或取消入口

位置：`static/tools/tools-slides-direct-upload.js:187–190`、`templates/tools_slides.html:67–74`。

新面板仅有按钮及文本状态，事件回调只处理 created，忽略共享上传器已经提供的 progress 和服务端阶段。用户拖入 OME-TIFF 后在转换工具内点击上传，会回到“正在上传”但看不到进度的体验；工作台旧上传行的进度正常。

建议复用现有上传进度展示和取消交互，不新增另一套只显示成功/失败的面板。

## 本轮独立执行

- C3 页面六项 `sc / gt / nd / vm / ra / bi` 全部通过：SCN、通用 TIFF、NDPI、VMS、BMP/JPEG、BIF；包含合成夹具的 preserve/compact、保存及浏览器/原生字节一致性。保存对话框为 OPFS 替身。
- 工作台上传脚本通过：直传请求、字节进度、整页 OME 拖入、SVS 转换引导、MRXS 文件夹交接（接收窗口替身）。
- pytest：`test_upload_direct_class.py`、`test_slide_format_registry.py`、`test_ingestion_api.py`，88 passed。
- 额外真实 Chromium 故障探测得到上述 1–4 项，标准字节构造验证第 5 项。
- 真实 NDPI/SCN/BIF 做了有界读取的分流探测，本轮没有重新执行它们的完整大样本转换和全层像素验收。浏览器/原生一致不等于对源图像无损。
- 未重跑全套 pytest、真实 COS、Windows 或 macOS，不把已有报告当成本轮独立执行结果。

## 状态核对与后续

“六个新增格式直传仍开着”与注册表一致，但 SVS/MRXS/KFB/KFBF 已关闭直传（SVS JP2K 有例外）。不要把整套候选记录成“所有格式直传都开放”。

先修 1、2；一并补齐 3、4、5 和直传交互，再复跑对应复现。此轮应保留六个新格式的直传，直至分流及变体退路经真实样本核对；本审查没有替用户改变直传政策。

私有证据：本 worktree `.gate-tmp/review-20261007/`，含 `dogfood.cjs`、`dogfood-results.json`、`hybrid.ome.tif`、`sniff.cjs`、`sniff-results.txt`、`real-sniff.cjs`、`real-sniff.txt`、六份 C3 JSON/日志、workbench.log、backend-tests.log。凭据只用于一次性本地测试服务，不纳入仓库。
