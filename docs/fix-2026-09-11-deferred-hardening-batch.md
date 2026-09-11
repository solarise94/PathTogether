# fix-2026-09-11：遗留加固批次（fail-open 默认值与静默吞噬）

2026-09-11 整体 review 的遗留中危项。共同主题：fallback 方向错误或错误
被静默吞掉。每项都保持对外契约不变（返回码/异常类型/用户文案），只修
fail 方向与可见性。

## P1. session 无 role 默认 owner → 区分 AUTH 模式（app.py）

现状：`actor_identity()`（约 5221 行）与 `current_identity()`（约 5279 行）
的「session 无 role → role=owner」。AUTH_ENABLED=False（内网免认证）时这是
合法路径，**不变**；但 AUTH_ENABLED=True 时 role 缺失只可能是异常（session
写入 bug / 序列化问题），静默给最高权限。

改法：
- AUTH_ENABLED=True 且 session 中 role 缺失 → **降级为
  `user_store.ROLE_GUEST`（最低权限）并记一条节流 warning**（含
  session 是否有 user_id，不记 session 内容本身）。不是 401、不是抛错——
  只把「默认可信」改成「默认最低权」。
- AUTH_ENABLED=False → 维持 owner（免认证内网模式语义不动）。
- 实现前置验证（必须做）：grep 全部 `session["role"]` / `session.get("role")`
  的写入点，确认 AUTH_ENABLED=True 下所有合法登录/激活路径都会写入 role；
  若发现任何合法路径不写 role，停下来在报告中说明，不要硬改。
- 更新两处 docstring（不再写「保守放行」）。

## P2. 预算对账读失败绕过 CAS → 跳过本轮（app.py 约 5991-5996）

现状：`_settle_terminated_run_budgets` 里 `get_reservation` 异常 →
`budget_row=None` → `expected_attempt=None` → consume/release 跳过乐观并发
检查（budget_store.py:703 `if expected_attempt is not None`），可能消费掉
已被新尝试接替的预留行。

改法：`get_reservation` 抛异常 → `app.logger.warning(..., exc_info=True)`
并 `continue` 跳过该 rid。对账由 `ai-budget-reclaim` 守护线程周期性执行，
本轮跳过、下轮重试，天然安全。**不允许**以 `expected_attempt=None` 继续。

## P3. 登录/改密的 hash 校验异常留痕（user_store_pg.py 约 470-474、585-588）

现状：`check_password_hash` 抛异常（hash 行损坏/scheme 不符）被吞，
用户只见「账号或密码错误」，服务端零日志——批量锁死与暴力破解无法区分。

改法：
- 模块引入 `logging.getLogger(__name__)`（参照 share_store_pg 的 `_LOG`
  惯例，含「不落敏感信息」注释）。
- 两处 `except Exception` 分支：返回行为不变（登录 None / 改密
  invalid_current_password），但记一条**节流**（300s，进程内简单实现 +
  测试复位 helper，仿照 share_store_pg `_audit_fail_log_last` 的写法）
  exception 级日志。日志只含 login_id 维度信息可省略——只记「hash 校验
  异常 + 堆栈」即可，不记密码、不记 hash 值。

## P4. Demo 安全限额读取失败静默回落 → 补日志（demo_store.py 约 155、app.py 约 4180）

现状：`demo_max_concurrency` / `_demo_task_max_steps` 读取失败静默回落实
装默认值（2 / 20）。默认值本身是设计上限、方向可接受，但「管理员调紧后
被静默放宽」完全无迹可查。

改法：只补可见性，**不改回落值**（2 并发 / 20 步已是保守内置上限）：
- `app.py:_demo_task_max_steps`：对齐兄弟函数 `_demo_public_mode` 的写法，
  `app.logger.warning("读取 Demo 步数上限失败（按默认 %d 处理）", ..., exc_info=True)`。
- `demo_store._demo_max_concurrency`：模块 logger 节流 warning（去掉
  `# pragma: no cover`，改为可测）。

## P5. spend_store.py 死代码清理（约 277-288）

`is_dispatch_maintenance_tx` 的 `except Exception: return True` 之后有不
可达语句 + 第二个重复 `except`，且死代码的缺 key 语义（`is not False` →
维护开）与活代码（缺键 → False 闸开）相反——留着是定时炸弹。

改法：删掉不可达语句与第二个 `except` 块，活代码一字不动；docstring 已
准确描述活语义（缺键 False / 异常 True fail-closed），不动。

## 不做

- 不改任何对外契约、错误码、用户可见文案；
- 不调整 ROLE_GUEST 的权限定义本身；
- 不改 demo 限额默认值；
- 不动 `_require_auth` 的白名单前缀（那是独立决策，不在本批）。

## 测试与验收

每项至少一条新测试，全部遵循 tests/ 现有 fixture 惯例：
- P1：AUTH_ENABLED=True + session 无 role → 得到 GUEST 而非 OWNER，且有
  日志；AUTH_ENABLED=False → 仍 OWNER（回归）。跑 test_access_control、
  test_preview 相关、test_owner_workspace_upgrade。
- P2：mock `get_reservation` 抛异常 → 该 rid 未被 consume/release（断言
  reservation 仍在），且有日志；下一轮（恢复 mock）正常结算。跑
  test_ai_budget_wiring / 预算对账相关测试。
- P3：monkeypatch `check_password_hash` 抛异常 → 登录返回 None + caplog
  有一条记录 + 窗口内第二次不再记；改密路径同理。
- P4：settings_store 读失败 → 返回默认值 + caplog 有记录。跑 demo 相关
  测试（test_demo_access 等）。
- P5：纯删除——现有 spend_store / dispatch maintenance 测试全绿即可。
- 最后跑 PT 全量 pytest（已知 test_e2e_pg_reap.py 的 2 个失败是沙箱
  环境问题，与本批无关）。
