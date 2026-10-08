# 测试入口与维护约定

在仓库根目录运行。Python 依赖安装在 `.venv`，前端依赖用 `npm ci` 安装。

```bash
.venv/bin/python -m pytest -q -ra
npm run test:js
PATH="$PWD/.venv/bin:$PATH" npm run test:e2e:admin
.venv/bin/python -m pytest plugins/pathtogether-baidu-import/tests -q -ra
```

- 默认 pytest 只收集 `tests/`。插件套件有自己的 conftest，单独执行；需要同时收集时可以显式传入两个目录。
- Vitest 只收集 `tests/js/**/*.test.ts`，不会把 Playwright spec 或历史验收脚本当作单元测试。
- PostgreSQL 是唯一后端。conftest 自动启动临时内嵌 PG、应用迁移、逐用例清表。无需 `RUN_PG_TESTS`，不要导入 conftest 常量判断后端。缺少必需 PG 依赖应当失败。
- 浏览器套件自动启动隔离主站与分享服务。默认端口为 8907/8908；可用 `E2E_PORT` 换一组空闲端口。不要指向生产数据库或复用生产应用进行测试。
- 缺少 native CLI、专用切片样本时的条件跳过仍保留。`pytest -ra` 显示原因；这些结果是未运行，不能算通过。构建 CLI 的入口见 `scripts/build_slide_transform.sh`。

## 公共夹具

Python 的 `_pt_helpers.make_client(auth=...)` 复用 Flask client 与真实 CSRF 双提交行为；`auth=None` 保持当前认证设置。先通过 `isolate_app` 或等价 fixture 建立隔离，避免认证状态泄漏。`pg_connection()` 连接当前测试库并返回字典行；调用方仍负责关闭连接及事务。

JS 的 `helpers/basic-element.ts` 只供不触发页面初始化的认证/API 测试使用。它是惰性元素桩，不模拟浏览器布局、事件传播或 canvas。需要这些行为的测试保留专用 harness，或使用 Playwright。

COS 测试使用真实上传引擎及 WebCrypto。假时钟只控制重试/轮询；等待真实可观察状态，不假定固定次数的微任务循环足以完成摘要。teardown 取消并等待上传任务，避免失败后串扰下一用例。

## 浏览器覆盖边界

`seed_assets.py` 将真实 TIFF/BMP 字节通过生产发布函数写入临时库和目录。BMP 主站测试验证瓦片、画布像素、无标尺语义、像素标注与刷新回放。分享页使用独立资产，不依赖另一个测试先成功。

这些测试不覆盖真实 COS 网络上传。上传路由、签名、权限、重试与完成回执分别由 Python 与 JS 套件验证；真实云端传输需要配置专用集成环境。不要为了通过离线 E2E 给生产应用添加测试专用上传端点。

首页动画通过可控 RAF 帧时钟驱动真实动画回调，观察全部 8 个自动场景；保留原生时钟的渐进文字、暂停/语言切换及移动端 reduced-motion 用例。

## 业务复现与 Opus 交接

基于 `admin-viewer@46fb4a5e`，`raster-image-compat.spec.ts` 中有两个带说明的 `test.fail` 用例。它们实际执行，不是 skip；报告中应单列为“预期失败”，不要合并成业务全部通过。

1. **分享选择器确认后浮层消失。** 打开分享 → 选择切片 → 确认。`confirmSlidePicker` 打开浮层后，同一次 click 冒泡到 `bindToolbarPop` 的 document 监听器又把它关闭。预期保留浮层供用户点击“分享选中切片”。
2. **仅有 slide_id 的新资产无法在分享页保存标注。** 通过生产发布路径创建资产，创建允许标注的分享，在分享页画箭头。路由按 ID 校验通过，`share_store_pg.add_roi` 却继续按空的 legacy filename 检查 `share.slides`，返回 400 `slide not in share`。应按稳定 ID 判断成员关系，同时保留其他资产越权保护。

Opus 修复后应删除对应 `test.fail` 标记，并完整跑通保存与刷新回放。此前 review 的业务实现也没有改动，但错误测试契约已在本分支纠正：

- `viewer-folders.test.ts`：到期时间统一用 epoch 秒，只放在 `/api/slides`，`/info` 不造额外字段；从有效窗口推进假时钟，验证到期后无人操作也清屏。搜索定位加入文件夹与切片混排；容量检查直接验证可见卡片数量，不复述有缺陷的分页公式。这三个用例暂用严格 `it.fails`。
- `test_admin_temporary_view.py`：保留管理员授权被撤销的成功用例，另加上传者同时持有 AI grant 的参数场景，要求管理员结束查看后上传者授权仍有效。新增场景用严格 `xfail`；不可把它解释为权限隔离通过。

因此共有 **6 个已知业务失败复现**：JS 3 个、Python 1 个、Playwright 2 个。修复业务代码后应去掉对应预期失败标记。Vitest/Playwright 的汇总会把这些计为符合预期，验收报告必须单独列出；它们仍是待解决的问题。

## 百度真实链路人工验收（原 L01–L04）

原 `test_baidu_live.py` 即使设置环境变量也始终 skip，没有可执行验收实现，已移出自动测试计数。保留以下人工检查；没有专用授权数据、账号和结果证据时统一记为 **NOT RUN**。

| 案例 | 条件与验收 |
| --- | --- |
| L01 只读枚举 | 专用分享至少两层目录，准备预期文件清单；枚举一致，过程中零转存、零下载、零删除。 |
| L02 选择后导入 | 一份 native 与一份支持转换的样本；真实下载/探测/转换后可打开，未选中的文件不转存。 |
| L03 中断恢复 | 非敏感、至少 1 GiB 文件；中途终止并重启 worker，恢复或有界重下，SHA 一致，产物及配额不重复。 |
| L04 副本清理 | 预置其他文件并注入清理故障；仅删除本批副本，保留预置文件，清理失败不改变已成功入库状态。 |

分享链接、提取码、令牌仅从专用测试环境读取，不写入仓库或验收日志。离线 fake 测试不能替代以上真实验收。
