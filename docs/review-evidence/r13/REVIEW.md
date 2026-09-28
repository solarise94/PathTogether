# R13：7ac4b37 独立审查

本轮独立运行指定七组测试：60 passed，40.25s。新增五个确定性反例：5 failed，1.25s。没有修改仓库业务代码或原有测试，没有生产操作；测试使用临时 PostgreSQL 和目录，没有真实 COS 请求。结论：R12 原反例覆盖的路径已有改进，但核账应用门禁与回执、V1 错误分支仍有缺陷，暂不接受“全部完成”。

## 1. [P1] 新 blocker 不阻止提交，归属漂移后先绑定再报告失败

定位：scripts/reconcile_upload_capacity.py:544–554（关联 _prestate_for/_verify_prestate）。

冻结时 A 用户任务指向 A 的未绑定预约；冻结后任务 owner 改为 B，行数、state、rid 均不变。预检不冻结 owner；重新分类已发现 mismatch_owner，但 blockers_now 被忽略，只检查新增 action key。随后仍按旧计划绑定并提交回执，应用后才返回 3。反例检查到预约已经绑定 B 的任务，费用仍记 A，回执也已插入。

修复：新 blocker 必须在任何写入前阻断；未执行动作须冻结并重验 owner、预约 owner/state/holder/purpose/金额、commit intent 等裁决字段，不只验证行数。应用事务内以锁定前态/CAS 再判定，回执与实际动作原子提交。no-go 用例必须同时断言数据和回执零变化。不要用事后退出码代替预检失败不写入。

## 2. [P1] 终态 consumed 被自动修复为第二份 reserved

定位：scripts/reconcile_upload_capacity.py:287–294。

consumed 仅在活跃任务分支被判为需要人工核实；同样记录位于 upload_task_terminal 时，--repair-residuals 直接建立新预约。真实 PG 反例：committed 任务、已 consume 100，暂存文件与 objects 文件为同一 inode。工具接受计划并应用，账本从 used=100/reserved=0 变成 used=100/reserved=100；尚未证明这是额外物理责任便重复占用用户容量。即使资产关系证据不足，也应 no-go，而不是自动收费。

scan_task_tree 的 seen 只在单个任务树内去重，不能证明与已发布对象/已结算源没有重复。修复：所有任务状态下 consumed 均须先解析结算和资产证据；无法解释的一律 blocker。只有证明是独立、未计账残留时才能补责任。共享 inode 的清理工作与容量责任分开，不为了让清理有 rid 就重复收费。

## 3. [P2] 冻结文件证据只有总数/总大小，等长替换仍通过

定位：scripts/reconcile_upload_capacity.py:392–402，关联 scan_task_tree:73–104。

冻结缺失预约任务的 100 字节暂存文件，把内容从 x*100 改成 y*100，应用仍返回 0 并执行 stop/repair。plan_hash 只保护计划 JSON，没有冻结文件内容、成员相对路径与类型；同数量等大小替换也无法检出。

静态核对还发现 os.walk 未设置抛错的 onerror，目录符号链接直接被过滤，而非报告证据不完整。这些行为不符合“扫描错误=no-go”。

修复：采用逐成员路径/类型/size/sha256 清单，核验所有依赖文件证据的动作（不只 repair）；目录遍历、成员类型/链接/读取失败显式阻断，不把少扫描等同于没有数据。将同大小内容变更、改名、不可读目录、目录 symlink 纳入门禁测试。

## 4. [P2] 清理后旧计划重跑，在查回执前就失败

定位：scripts/reconcile_upload_capacity.py:527–539。

首次修复成功后，正常删除暂存并调用 confirm_cleanup_and_release，再应用同一计划：返回 3，原因是文件证据由 100/1 变成 0/0。已应用回执还没读取，文件预检就提前失败。修完此顺序后，还要处理 _verify_prestate 把 stop 动作机械折算为永久 pending+1 的假设：正常清理会删除 pending，不能因此将已完成动作判为环境漂移。

修复：先解析并校验回执对应的动作结果，区分未执行、已执行仍持有、已合法结算/清理的生命周期；仅未执行动作要求原文件前态。对已应用动作验证完整合法后继关系，不能简单全跳过，也不能要求残留永远存在。回执应持久记录新 rid/应用结果，而不仅 action_key。重跑返回成功且不重新收费；同时测试真实的异常漂移仍被拒绝。

## 5. [P2] V1 原生上传 owner 缺失分支重复 flock 自锁

定位：app.py:12545–12554，关联 _upload_abandon_staging:12063–12067。

_api_upload_native_single 已持 upload_task 文件锁，owner 无法解析时调用会自行取同一锁的 _upload_abandon_staging。不同 open-file-description 的 flock 不可重入，默认 timeout=None 导致请求一直等待自身，原本应返回的 500 不会返回。

反例将 owner resolver 返回 None，给真实 flock 仅加 50ms 超时以免挂住测试；调用栈稳定为 native → abandon → 第二次 flock 超时。没有替换 flock 本身。相邻 unsupported entry 分支具有同样结构。

修复：锁内只调用明确的 under_storage_lock 收口，或把需要再取锁的收尾移到最外层锁外。按所有早退与异常分支审计，不能只用 COS writer 自失败用例证明所有通道无递归锁。

## 复现命令

PathTogether 根目录：

```bash
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r13/test_r13.py -q --tb=short
```

该目录 conftest 是当前 tests/conftest.py 的副本；只收集外部文件避免重复启动两套测试数据库。原始输出见 results.txt。没有重跑全量 Python/JS/HP，不将用户报告的全量结果冒充本轮验证。
