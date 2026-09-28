# R16 独立审查：COS 统一上传与主页引导

审查日期：2026-09-28。PathTogether HEAD：ca0f25e。只读产品代码；反例与结果均在本目录，未修改工作区已有改动、未提交、未部署、未开启 capability。

结论：当前交付尚不满足上线门禁。确认 4 项 P1、2 项 P2；其中 5 项有运行反例，检查点 A 缺依赖由固定提交源码与 Containerfile 对照确认（未构建 Docker 镜像）。

## 1. [P1] KFB 在 intent 前取消，留下可执行子任务与无账源文件

位置：cos_ingest_worker.py:1335–1397，重点为 intent 被拒后的 1392–1397。

先 ensure_conversion_job 创建 queued 转换任务并搬走源文件，之后才 worker_persist_commit_intent。两步之间取消可正常提交；intent 拒绝后代码仅释放租约、返回，取消清理只覆盖 ingestion 暂存树。转换任务拥有自己的目录，未被撤销，也没有完成源字节结算。

反例使用真实 PG 和真实 store：在 intent 调用前执行实际取消短事务，worker 退出文件锁后执行实际取消清理。最终父任务 cancelled，用户账本 reserved=0 / used=0，子任务仍 queued，conversion staging 内 data.kfb 仍在。它仍可被转换 worker 领取。

修复要求：建立明确的事务性转交协议。在子任务成为可领取状态前，原子记录父任务提交栅栏、父子关联及容量责任归属；取消先赢则不得创建可执行子任务，提交先赢则取消走 commit_in_progress。若采用补偿，必须持久化待补偿工作并覆盖崩溃重试，不能仅在异常分支尽力删除。不得株连幂等复用的历史子任务。补验两种胜者及交接各落点崩溃。

## 2. [P1] ZIP 解压先落盘、后补占，违反容量生命周期合同

位置：upload_content.py:355、404–415。

prepare_zip_bundle 完整解压所有成员后才 topup；补占目标只考虑展开字节，没有计入仍存在的压缩源。方案明确要求下一段增加字节写入前补齐容量责任。磁盘水位不替代用户配额预占。

反例：531 字节压缩包展开为 20,000 字节；首次调用 topup 时真实暂存文件总量已经 20,531 字节，预约仍仅 531 字节。即使最终配额不足并清理，用户也已经绕过额度限制消耗了磁盘；清理失败时缺口继续存在。

修复要求：以源文件与展开文件同时存在的峰值计量，在写入下一有界块之前补占；可先安全解析成员声明预占，但仍需实际字节守卫。清理失败保留完整责任，发布与源清理分别确认结算，不能把最终产物大小当峰值预算。补验小配额高压缩率、写前拒绝、部分解压后清理失败、源与产物同时存在。

## 3. [P1] 排空 report 对已知终态任务残留返回 go

位置：scripts/upload_drain.py:129–149。

known 集合包含所有历史任务，暂存扫描只检查目录名是否在集合中；存在任务行就免检，没有核对终态、责任与实际残留。pending/reservation 维度也不会捕获无 rid/pending 的终态残留。

反例：创建并取消旧任务，再构造该任务目录中的 100 字节历史残留，无 pending、无预约。report 输出“旧链路责任全部收口”，返回 0，预期应为 3。该状态是已有核账合同要求检测的存量异常，不能因为取消正常路径会清理而排除。

修复要求：复用权威核账/文件证据扫描，按状态、责任、文件三方关联裁决；已知任务只能提供归属线索，不是残留合法证据。旧任务残留无论有无 rid/pending 都应阻断 B。检查 A 可部署版本也包含修复。补验终态矩阵、扫描失败、正常空目录放行；若依赖另一份核账报告，必须机器校验其时点与对应冻结证据，不要仅靠文档串联。

## 4. [P1] 固定检查点 A 镜像漏装 upload_content，无法作为先行版本部署

位置：docs/cos-only-upload-delivery-report-20260928.md:10；9d8d2b0:Containerfile 与 9d8d2b0:app.py:269。

报告要求以确切提交 9d8d2b0 构建 A，但该提交 app.py 顶层 import upload_content，Containerfile 显式 COPY 列表没有这个文件，也没有复制整个仓库。干净镜像缺少模块，app 导入会失败。acb1e8e 的补 COPY 只修复后续代码，不能改变历史 A 镜像。

修复要求：从 A 衍生新的兼容排空候选提交，带齐依赖及必要排空修复，更新交付报告的确切 SHA/镜像标识；不能把已删除旧恢复路径的 B 当作 A。对新 A 做实际镜像启动、迁移和冻结旧任务恢复/取消冒烟。检查结果见 checkpoint-a-dependency.txt；本轮没有冒充执行过容器构建。

## 5. [P2] ZIP intent 前重试重复分配无绑定资产

位置：cos_ingest_worker.py:1136–1153。

allocate_slide+item 绑定先事务提交，intent 另开事务。如果中间失败，下次无 intent 会重新分配 slide_id；bind_ingestion_job_item 冲突时保留旧绑定，但新分配资产已提交，调用方也未使用返回的既有绑定。

反例：一次 intent 失败后重试，一个 ZIP item 生成一条 ready 与一条无绑定 staging 资产。每次同窗口失败都会累积，现有任务清理不负责这些新分配的孤立资产。

修复要求：item 分配/绑定/父 intent 同事务，锁内复用已存在 item，只有缺项才分配；事务失败不得留下新资产。覆盖重试、真实提交前崩溃及取消，断言资产集合与 item 绑定严格一致。

## 6. [P2] closed 模式的“如何开始”仍宣称可以申请注册

位置：templates/entry.html:152–158。

注册模式分支只有 invite_only / public / else，closed 落入 email_verify 文案：“验证邮箱并提交申请，管理员审核通过后即可使用”。与 Hero 的“暂未开放注册”矛盾，恰好影响本次要解决的新用户使用引导。

实际 GET / 的 closed 模式反例已复现。修复应给 closed 独立步骤与说明，引导已有账号登录或体验 Demo，不承诺当前不可用的申请路径；中英文及状态矩阵同时更新，断言整个引导区而非仅 Hero 链接。

## 测试与门禁纠偏

- 本轮复跑既有 COS kinds / drain / auth UI / demo 四文件：112 passed，见 baseline-results.txt。
- 新增 COS/排空独立反例：4 failed，均为预期不变量被打破，见 test_r16.py、results.txt。
- 主页反例：1 failed，见 test_homepage_r16.py、retained-and-homepage-results.txt。
- R13 整文件 skip 不合理：4 个核账反例保持原断言运行仍通过；只有 native_upload_owner_failure 依赖已删除 V1 helper，应仅退役该例。临时去掉整个 skip 后尝试运行全部 5 例的结果保留在 r13-retained-results.txt（4 passed、已退役 V1 用例因 helper 删除失败）；再以 -k 排除该例运行，4 passed / 1 deselected。
- 未重跑全量 pytest、全量浏览器或真实 COS/MRXS/CSP/SMTP；用户报告的全量数字不作为本轮独立验证结果。Playwright 的 8 个遗留失败仍需逐项映射或修复，尤其需补真实注册/登录到统一上传完成的连贯测试，不能以抽屉能打开替代。

复跑命令（仓库根目录）：

```bash
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r16/test_r16.py /tmp/slide-id-review-r16/test_homepage_r16.py -q --tb=short
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r16/test_r13_retained.py -k 'not native_upload_owner_failure' -q --tb=short
```

所有反例使用临时 PG、本地合成字节；复用既有 FakeCos 和格式探测替身，未访问生产或真实桶。取消反例没有在持文件锁时递归调用自取文件锁的公开取消函数：真实取消短事务在 writer 持锁时提交，清理阶段在 writer 退出后运行，精确对应实际允许的时序。
