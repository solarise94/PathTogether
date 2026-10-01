# R1 r1-rc3：上线 dogfood 缺陷修复（2026-10-02）

基线：r1-rc2（1c534c4，2026-10-01 21:18 上线）之后的 release/r1（daac63d）。来源：
2026-10-01 生产 dogfood 报告（2 个 P1、4 个 P2；报告本体含测试账号/切片标识，不入库）。
本候选只改应用代码与测试，不含迁移、配置、开关或部署工具改动。

## 缺陷 → 修复 → 回归

| 缺陷 | 根因 | 修复 | 回归（修复前失败已验证） |
|---|---|---|---|
| P1 从项目打开新上传切片 → `GET /api/slides/<文件名>/info` 404 | 项目 DTO 只有 `slides`（名快照）与过滤掉空值的 `slide_ids`，两者不能逐行配对；客户端按名找列表项，id_bundle 资产 `name` 为空 → 「读取失败」并以文件名作身份 | 项目 DTO 增加逐行对齐的 `slide_refs: [{slide, slide_id}]`；项目行以 slide_id 为身份（旧后端无该字段时回落按名） | pytest `test_project_slide_refs_are_row_aligned_with_ids`；e2e `l1-project-open-and-ui-delete`（刷新后从项目点开，info 必须是 `/api/slides/<id>/info` 200） |
| （e2e 发现）不带 Idempotency-Key 按 `slide_ids` 建项目：同原始文件名的 id_bundle 切片并成一行、ID 丢失 | 创建端点把 `original_filename` 当名快照走按名建行，只有带键路径才回写 ID | 创建成功后无论是否带键都以 ID 为权威补写项目行（`update_project`，成员集与顺序不变）。工作台「新建项目」带键，原本不受影响；其他不带键调用方受影响 | pytest `test_project_create_by_ids_keeps_same_filename_id_bundle_slides[None/k-dup-same]` |
| P1 界面删除 → `DELETE /api/slide/<名>` 403「无权访问」 | 删除按钮仍走旧名端点 | 有 slide_id 时走 `DELETE /api/slides/<id>`（旧后端/无 ID 行才回落按名）；关闭当前查看器按 ID 比对；无唯一名的 id_bundle 资产不显示按名操作的 Demo 按钮 | e2e `l1`：普通用户、两张同原始文件名切片，只删所点那张，请求为 ID 端点 200，另一张仍可读 |
| P2 「本机转换并上传」被导入抽屉遮罩挡住 | 入口只渲染在侧栏上传行 | 抽屉打开时同时在抽屉「本机」面板顶部给出入口（含忽略），点击仍由用户手势打开 popup；交接成功后移除 | e2e `l3-drawer-offer-clickable`（抽屉不关，Playwright 遮挡检查下点击成功并完成交接）；vitest |
| P2 未处理的 KFB 入口 60s 后消失 | 待处理动作沿用了完成通知的 `finish(60000)` | 入口保留到用户转换或点「忽略」；popup 被拦截时也不再定时移除（可重试，工具页链接只追加一次） | e2e `l2-convert-offer-persists-past-60s`（page.clock 快进 71s，不点击）；vitest 3 例 |
| P2 格式目录仍写「上传后后台转换」，英文界面混入中文 | 服务端目录与前端回退目录文案过时；描述未走 i18n | 服务端/回退目录改为「在本机浏览器中转换…后上传」；徽章「本机转换后上传 / Convert in browser, then upload」；名称与说明按格式 id 走 i18n（中英） | `test_slide_format_registry`、vitest i18n 契约 |
| P2 隐私文案无条件声称文件不离开设备 | 「本地转换」承诺未限定范围 | 中英文分开表述：仅本地转换时不出设备、不建任务；授权上传后创建上传任务，只发送转换产物及其文件名、大小，原始文件不上传；登录/离线承诺同样限定到本地转换 | `test_slide_tools_page` |

## 已知、未改动的语义

删除切片不解除其项目归属（legacy 与 ID 删除编排都不改 `project_slides`）。删除后项目中该行显示为「读取失败」，同名的另一张切片不受影响。是否改为删除时自动移出项目，需单独决定；本候选不改。

## 门禁（release/r1 工作树，均串行）

| 门禁 | 结果 | 证据 |
|---|---|---|
| 全量 pytest（`tests/`，TMPDIR 指大盘，deselect 已知 1 例） | 2865 passed，3 failed（`test_admin_preview.py`，与 rc2 记录相同、在未改动 HEAD 上复现），10 skipped | `results/pytest-full-rc3.txt` |
| 插件 pytest（baidu-import） | 83 passed | 同上 |
| vitest | 611/611 | `results/vitest-rc3.txt` |
| R1 浏览器 e2e | 27/27 ALL PASS（含新增 l1–l3） | `results/r1-e2e-rc3.txt` |
| C4 浏览器 e2e | 15/15 ALL PASS | `results/c4-e2e-rc3.txt` |
| C3 浏览器 e2e（仅转换：隐私/离线） | 18/18 ALL PASS | `results/c3-e2e-rc3.txt` |

## 部署后真实用户验收（生产，普通用户）

1. 上传一张切片并加入项目（含一张与已有切片同原始文件名的）；刷新页面，从项目展开并打开——info 请求为 `/api/slides/<id>/info` 且 200，无「读取失败」。
2. 在项目内通过界面删除其中一张——请求为 `DELETE /api/slides/<id>` 200，提示「已删除」，同名另一张仍能打开。
3. 导入抽屉打开时选择 KFB——抽屉内出现「本机转换并上传」，不关抽屉即可点击并完成交接；放置超过 60 秒入口仍在。
4. 中英文导入抽屉格式目录与工具页隐私说明与上述文案一致。
5. 验收数据按 dogfood 惯例清理（ID 端点删除、删除临时项目）。
