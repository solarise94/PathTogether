# fix-2026-09-11：record_audit 静默吞噬修复

## 问题

`share_store_pg.py` `record_audit()`（约 1888-1890 行）：

```python
    except Exception:
        return False
```

吞掉一切异常（PG 故障、`audit_events` 表缺失、权限错误），**无任何日志**。
而调用方以为它有异常抛出并写了兜底日志，例如
`app.py:16073-16076`（`plugin_capability_dispatch` 审计）：

```python
        try:
            share_store.record_audit(...)
        except Exception:
            app.logger.warning("dispatch 审计写入失败（best-effort）", exc_info=True)
```

由于 `record_audit` 永不抛出，这些 `except` 是**不可达死代码**
（同样模式见 app.py:11425-11427、14808 附近，以实际搜索为准）。
后果：审计管道整体坏掉时 100% 丢审计事件且零信号——插件安装/卸载、
run grant 吊销、capability dispatch 等安全事件无迹可查。

对照：同文件的 `record_audit_tx`（约 1893 行）是 fail-loud 的，说明
「吞掉」只是 `record_audit` 独有问题，不是模块惯例。

## 设计

只改 `record_audit` 内部，**不改它的对外契约**（仍返回 bool、仍不抛出——
审计是 best-effort，不能反过来打挂业务路径）：

1. 文件顶部引入模块级 `logger = logging.getLogger(__name__)`（若已有则复用）。
2. `except Exception:` 分支改为：节流地记录 `logger.exception(...)`
   ——避免 PG 故障期间每次审计写都刷一条堆栈。节流器用进程内简单实现
   （模块级 `{"last": 0.0}` + 间隔常量 300s；若本文件/项目已有同类节流
   helper——如 app.py 的 `_warn_secret_throttled` 模式——优先复用风格，
   但不要为这一个点跨文件抽象新公共件）。
3. 日志内容：固定说明文本 + `exc_info`，**不含** action 参数里的敏感负载
   （audit detail 可能含业务数据；记 action 名与 target_type/target_id
   即可，不记 detail）。

调用方的死代码 `try/except`（app.py 若干处）**保留不动**——它们无害，
且未来若 `record_audit` 契约变为可抛出，这些兜底依然正确。本批不清理，
避免无关 diff。

## 不做

- 不改返回值/抛出语义；
- 不改 `record_audit_tx`；
- 不引入审计写入失败后的重试/落盘队列（那是新特性，另议）。

## 测试与验收

- 新增/扩展测试（找现有 record_audit/审计相关测试文件，遵循其惯例）：
  人为制造写入失败（如 mock/拆掉表依赖），断言：返回 False、且
  `caplog` 收到一条 exception 级日志、且第二次失败在节流窗口内不再记录。
- 相关测试全绿；`git diff` 只触及 `share_store_pg.py` 与测试文件。
