# R15：b984c5a 独立复核

R14 三项反例及 R13/核账回归共 33 passed（4.33s）。新增两条完整流程反例在真实临时 PG 上复现：2 failed（0.67s）。未改仓库、未运行生产核账、未访问真实 COS；没有重跑全量 Python/JS/HP。

## 1. [P1] 正常 owner 上传被核账当成失去预约的任务终止

定位：scripts/reconcile_upload_capacity.py:211–213；对照 app.py:_upload_acquire_reservation_exact 与 upload_guard.py:quota_applies。

V1/V2 的 owner/本地身份按既有合同不需要容量预约，但 collect 对 upload_tasks 没有身份豁免判定。它只有 COS ingestion_job 分支会按 owner_role 产生 exempt 类型。普通 owner 上传会被分类为 missing，然后生成 stop；有残留时 --repair-residuals 还可能为本不需要预约的任务补收费责任。

反例通过真实 create_user(role='owner')、登录 session 和 POST /api/uploads 创建任务，API 返回 200、任务 active、rid=None，均为正常状态。执行冻结计划和应用（没有篡改 DB 初态），两步返回 0，任务却变成 failed 并出现 cleanup pending。

修复要求：普通上传与 COS 使用一致且可证明的配额身份合同。不要凭 rid=None 推断豁免，也不要只查当前角色忽略任务创建后身份变更；必要时持久化任务创建时的配额模式/主体，旧数据用可信配置和审计证据裁决，无法证明时 no-go 而不是 stop。分别覆盖 owner、普通用户、本地模式以及角色变更：合法豁免任务不应生成 stop/repair，不应改变状态或收费；普通用户确实失去预约仍被检出。

## 2. [P2] 新发现的终态残留补账后，没有持久清理工作

定位：scripts/reconcile_upload_capacity.py:713–727。

R14 已将终态无 rid/pending 残留纳入集合，但 repair 的应用实现仍假设 pending 已存在：对 upload_cleanup_pending 只 UPDATE，不 INSERT。终态任务不会生成 stop，因此没有前置步骤创建清理行。

反例：failed + rid=None + 无 pending + 实际 100 字节，--plan-out/--apply --repair-residuals 均返回 0，新 reserved 责任已绑定，任务保持 failed，但 get_cleanup_pending 返回 None。容量被保留却没有可重试的清理工作，不能达到“补责任并安排清理”的闭环。静态核对 COS 同分支仅更新 local_reservation_id，不把原 none/cleaned 的 local_cleanup_status 置 pending，也需同类处理。

修复要求：把 repair 定义为同事务建立残留责任、绑定任务、保证对应清理工作存在。V1/V2 用幂等 upsert，COS 建立/重置本地 pending 与相应重试资格，不能改动无关远端清理结果。回执重跑不重复补账；应用后终验除账平与绑定外，还要验证每份待清残留可由持久清理器领取。增加两通道“发现→补记→清理故障保留→恢复清理→恰一次释放→同计划重跑”全链路验收，不只断言 dry-run 返回 3。

## 复现

PathTogether 根目录：

```bash
PYTHONPATH=.:tests:scripts .venv/bin/python -m pytest /tmp/slide-id-review-r15/test_r15.py -q --tb=short
```

外部 conftest 为当前 tests/conftest.py 副本；单独收集 test_r15.py。结果 results.txt。新增反例覆盖正常 owner API 创建和终态修复应用，均不依赖生产凭证。

本轮 R14 定点修复通过，但维护核账仍不应上线：先补齐身份合同和 repair 收尾，再进行停写副本演练及生产独立门禁。
