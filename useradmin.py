# -*- coding: utf-8 -*-
"""主机侧账户管理 break-glass CLI（账户系统批次 A，docs §7.3）。

用法（在仓库根目录执行）::

    python3 -m useradmin reset-owner-password \\
        --login-id browser_admin \\
        (--password-stdin | --password-file PATH) \\
        [--enable]

行为约定：

- 连接：只读 ``DATABASE_URL`` 环境变量，经 ``pg_store.connect()`` 直连
  PostgreSQL；未设置或连接失败 → stderr 报错 + 非 0 退出。**不依赖
  STORAGE_BACKEND**（主机侧工具等价于直接改库，数据库本身就是唯一事实源）；
- 目标定位：``lower(email) = lower(trim(--login-id))`` 且 ``role='owner'``；
  且库内 ``role='owner'`` 行总数必须**恰好为 1**（0 个或多个都拒绝，绝不选
  「第一条」）；
- 目标为被禁用的唯一 owner 时必须显式 ``--enable``：同一事务内解除禁用 +
  更新 hash + auth_version+1；目标已启用却带 ``--enable`` → 报错退出（语义
  模糊拒绝）。0 个 owner 行 → 拒绝并输出逃生路径，**绝不静默建号**；
- 密码只从 stdin（TTY 经 getpass 无回显读一次；管道读全部）或
  ``--password-file``（去单个尾部换行）读取，拒绝命令行参数值、空/全空白；
  执行统一 15..200 策略（常量取自 ``user_store_pg``，与 store 层单一来源）；
- audit：同一事务写 ``audit_events``，action=
  ``user.password_break_glass_reset``，actor_user_id=NULL，detail JSONB 为
  ``{"source":"local_cli","sessions_revoked":true}``（``--enable`` 时追加
  ``"reenabled":true``）；
- 成功输出 user_id、login_id 与「旧 session 已失效」；任何输出/异常不含
  密码或 hash。

退出码：0 成功；1 操作拒绝/失败；2 参数用法错误（argparse 默认）。
"""

import argparse
import os
import secrets
import sys
import time

import psycopg

import pg_store

# 统一密码策略常量直接取自 PG store 实现（与 user_store dispatcher 导出同源；
# 不 import user_store 本体——dispatcher 会按 STORAGE_BACKEND 触发 json 模块的
# SHARE_DATA_DIR 建目录等副作用，主机侧工具不应依赖它）。
from user_store_pg import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH

#: break-glass audit 动作名（docs §7.3；detail 只含 source/sessions_revoked/reenabled）
AUDIT_ACTION = "user.password_break_glass_reset"

#: 0 个 owner 行时的逃生路径说明（docs §7.3：不得静默建号）
_NO_OWNER_ESCAPE_HINT = (
    "逃生路径（人工操作，CLI 不会代为执行）：\n"
    "  1. 人工审计后用 SQL 直接恢复 owner 行（基于备份的 user_id/email/"
    "password_hash，恢复后先用本人改密或再次 break-glass 换掉恢复的 hash）；或\n"
    "  2. 确认并清空整个 users 表后，走 bootstrap secret（空库首建，"
    "BOOTSTRAP_OWNER_LOGIN_ID + BOOTSTRAP_OWNER_PASSWORD_FILE）重新引导。\n"
    "详见 docs/account-system-simplification-fix-plan.md §7.3。"
)


def _err(msg):
    """错误一律走 stderr（成功信息才进 stdout）。"""
    sys.stderr.write("useradmin: 错误：%s\n" % msg)


def _strip_trailing_newline(text):
    """去掉单个尾部换行（\\r\\n / \\n / \\r）；密码中间/末尾其它空白保留。"""
    if text.endswith("\r\n") or text.endswith("\n\r"):
        return text[:-2]
    if text.endswith("\n") or text.endswith("\r"):
        return text[:-1]
    return text


def _read_password_stdin():
    """--password-stdin：TTY 下经 getpass 无回显读一次；非 TTY 读全部。"""
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("请输入新密码（输入不回显）：")
    return _strip_trailing_newline(sys.stdin.read())


def _read_password_file(path):
    """--password-file：读文件内容并去单个尾部换行；读失败报错退出。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _strip_trailing_newline(f.read())
    except OSError as exc:
        raise _CliError("无法读取密码文件 %s：%s" % (path, exc))


def _validate_password(password):
    """统一密码策略：非空、非全空白、15..200 字符。违规抛 _CliError。

    消息只含长度，不含密码本身（docs §3.3）。
    """
    if not isinstance(password, str) or not password:
        raise _CliError("密码不能为空")
    if not password.strip():
        raise _CliError("密码不能为全空白字符")
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        raise _CliError(
            "密码长度须在 %d..%d 字符之间（当前 %d 字符）"
            % (PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH, len(password))
        )
    return password


class _CliError(Exception):
    """CLI 可预期失败（已带用户可读消息，直接 stderr + exit 1）。"""


def _connect_pg():
    """按 DATABASE_URL 建连接；未设置/失败抛 _CliError（不打印 DSN 本身）。"""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise _CliError(
            "未设置 DATABASE_URL 环境变量。break-glass CLI 直连 PostgreSQL，"
            "请提供与应用一致的 DATABASE_URL。"
        )
    try:
        conn = pg_store.connect()
    except Exception as exc:
        raise _CliError("PostgreSQL 连接失败（DATABASE_URL 已设置）：%s" % exc)
    conn.row_factory = psycopg.rows.dict_row
    return conn


def _cmd_reset_owner_password(args):
    """reset-owner-password 子命令实现（docs §7.3）。返回进程退出码。"""
    from werkzeug.security import generate_password_hash

    if args.password_stdin:
        password = _read_password_stdin()
    else:
        password = _read_password_file(args.password_file)
    _validate_password(password)

    conn = _connect_pg()
    try:
        with pg_store.transaction(conn) as c:
            with c.cursor() as cur:
                # owner 行总数必须恰好为 1：0 个/多个都拒绝，不选「第一条」
                cur.execute("SELECT count(*) AS n FROM users WHERE role='owner'")
                n_owners = int(cur.fetchone()["n"])
                if n_owners == 0:
                    _err(
                        "库中不存在任何 role='owner' 行，拒绝执行。\n"
                        + _NO_OWNER_ESCAPE_HINT
                    )
                    return 1
                if n_owners > 1:
                    _err(
                        "库中存在 %d 行 role='owner'（要求恰好 1 行）；"
                        "须人工审计收敛到唯一 owner 后再执行。" % n_owners
                    )
                    return 1

                cur.execute(
                    "SELECT user_id, email, disabled FROM users "
                    "WHERE lower(email) = lower(trim(%s)) AND role='owner'",
                    (args.login_id,),
                )
                target = cur.fetchone()
                if target is None:
                    _err(
                        "--login-id %r 与库中唯一 owner 的登录账号不匹配"
                        "（大小写不敏感、两侧均做 trim）。" % args.login_id
                    )
                    return 1

                reenable = bool(target["disabled"])
                if reenable and not args.enable:
                    _err(
                        "目标是禁用状态的唯一 owner：必须显式加 --enable，"
                        "CLI 才会在同一事务内解除禁用并重置密码。"
                    )
                    return 1
                if not reenable and args.enable:
                    _err(
                        "目标 owner 当前已启用，无需 --enable"
                        "（该选项仅用于重置被禁用 owner 时同事务恢复启用）。"
                    )
                    return 1

                # 同一事务：hash 更新 + auth_version+1（+ 解除禁用）+ audit
                if reenable:
                    cur.execute(
                        "UPDATE users SET password_hash=%s, disabled=FALSE, "
                        "auth_version = auth_version + 1 "
                        "WHERE user_id=%s "
                        "RETURNING user_id, email, disabled, auth_version",
                        (generate_password_hash(password), target["user_id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE users SET password_hash=%s, "
                        "auth_version = auth_version + 1 "
                        "WHERE user_id=%s "
                        "RETURNING user_id, email, disabled, auth_version",
                        (generate_password_hash(password), target["user_id"]),
                    )
                row = cur.fetchone()

                detail = {"source": "local_cli", "sessions_revoked": True}
                if reenable:
                    detail["reenabled"] = True
                cur.execute(
                    "INSERT INTO audit_events "
                    "(event_id, ts, actor_user_id, actor_role, action, "
                    " target_type, target_id, slide, detail) "
                    "VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s)",
                    ("aud_" + secrets.token_hex(16), time.time(),
                     None, "", AUDIT_ACTION, "user", row["user_id"], None,
                     psycopg.types.json.Jsonb(detail)),
                )
        # 事务提交成功后才输出（任何拒绝路径都在事务内 return，触发 rollback）
        print("OK：owner 密码已重置（break-glass）")
        print("user_id:  %s" % row["user_id"])
        print("login_id: %s" % row["email"])
        if reenable:
            print("已同时解除禁用（disabled=false，reenabled=true）")
        print("旧 session 已失效：该账号全部已登录会话即刻作废，需用新密码重新登录。")
        return 0
    finally:
        conn.close()


def _build_parser():
    """构造 argparse（子命令结构便于后续扩展其它主机侧账户操作）。"""
    parser = argparse.ArgumentParser(
        prog="python3 -m useradmin",
        description="PathTogether 主机侧账户管理 break-glass CLI（详见模块 docstring）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rp = sub.add_parser(
        "reset-owner-password",
        help="重置唯一 owner 的密码（忘记密码的最后恢复手段）",
    )
    rp.add_argument(
        "--login-id", required=True,
        help="唯一 owner 的登录账号（大小写不敏感，两侧做 trim）",
    )
    grp = rp.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--password-stdin", action="store_true",
        help="从 stdin 读新密码（TTY 经 getpass 无回显；管道/重定向读全部并去单个尾部换行）",
    )
    grp.add_argument(
        "--password-file", metavar="PATH",
        help="从文件读新密码（去单个尾部换行；文件权限应仅限当前用户可读）",
    )
    rp.add_argument(
        "--enable", action="store_true",
        help="目标为被禁用的唯一 owner 时：同事务解除禁用+重置密码+auth_version+1",
    )
    rp.set_defaults(func=_cmd_reset_owner_password)
    return parser


def main(argv=None):
    """CLI 入口（返回进程退出码；供 ``python3 -m useradmin`` 与测试直接调用）。"""
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _CliError as exc:
        _err(str(exc))
        return 1
    except psycopg.Error as exc:
        # 数据库异常：只透出类型与消息（psycopg 消息不含密码/hash）；
        # 客户端侧错误（如连接中断）没有 server diag，回退到 str(exc)
        diag = getattr(exc, "diag", None)
        msg = getattr(diag, "message_primary", None) or str(exc)
        _err("数据库操作失败：%s: %s" % (type(exc).__name__, msg))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
