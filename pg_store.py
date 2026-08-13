# -*- coding: utf-8 -*-
"""PostgreSQL 连接与迁移基建（Stage 3b-1）。

本模块只提供「连接 + 事务 + 迁移执行器」三件套，**不**含任何业务仓储实现——
那是 Stage 3b-2 的事。本节点交付后：

- 运行期依赖 ``psycopg[binary]>=3.2``（见 requirements.txt）；
- 测试基建依赖 ``pgserver``（见 requirements-dev.txt），用其内嵌的真实 PG 二进制
  跑迁移与外键冒烟（tests/test_pg_infra.py）；
- dispatcher（share_store.py / user_store.py）目前**不** import 本模块；postgres/
  dual 后端的真正接入在 3b-2。

风格对齐 share_store.py：模块级函数 + 中文注释。
"""

import contextlib
import os
from pathlib import Path

# 迁移脚本目录（<repo_root>/migrations）
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# psycopg3 延迟可用性探测：缺依赖时给出清晰中文错误，而非裸 ImportError。
try:
    import psycopg  # type: ignore
except ImportError as _exc:  # pragma: no cover - 仅缺依赖时触发
    psycopg = None
    _PSYCOPG_IMPORT_ERROR: Exception | None = _exc
else:
    _PSYCOPG_IMPORT_ERROR = None


# 迁移记录表 DDL（幂等）。ensure_schema 第一步先确保它存在。
_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _require_psycopg():
    """psycopg 不可用时抛中文 RuntimeError。"""
    if psycopg is None:  # pragma: no cover - 仅缺依赖时触发
        raise RuntimeError(
            "缺少 psycopg 依赖：请安装 psycopg[binary]>=3.2"
        ) from _PSYCOPG_IMPORT_ERROR
    return psycopg


def get_conninfo():
    """构造 libpq 连接串。

    优先级：
      1. ``DATABASE_URL`` 环境变量（psycopg3 可直接吃 URL）；
      2. ``PGHOST`` / ``PGPORT`` / ``PGUSER`` / ``PGPASSWORD`` / ``PGDATABASE``
         组合，拼成 ``key='value'`` 形式（值中单引号翻倍转义）；
      3. 两者都缺 → 抛 RuntimeError（中文提示）。
    """
    url = os.environ.get("DATABASE_URL")
    if url and url.strip():
        return url.strip()

    env_map = (
        ("PGHOST", "host"),
        ("PGPORT", "port"),
        ("PGUSER", "user"),
        ("PGPASSWORD", "password"),
        ("PGDATABASE", "dbname"),
    )
    pairs = []
    for env_key, libpq_key in env_map:
        val = os.environ.get(env_key)
        if val:
            # libpq keyword=value：值用单引号包裹，内部单引号翻倍
            escaped = "'" + val.replace("'", "''") + "'"
            pairs.append("%s=%s" % (libpq_key, escaped))
    if not pairs:
        raise RuntimeError(
            "未配置 PostgreSQL 连接信息：请设置 DATABASE_URL，或 "
            "PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE 环境变量。"
        )
    return " ".join(pairs)


def connect():
    """建立新连接（autocommit=False，事务由调用方/transaction() 管理）。"""
    _pg = _require_psycopg()
    return _pg.connect(get_conninfo(), autocommit=False)


@contextlib.contextmanager
def transaction(conn):
    """事务上下文：正常结束 commit，异常 rollback，**不关闭连接**。

    注意：psycopg3 的 ``Connection.__exit__``（即 ``with conn:``）在 commit/rollback
    之后会**关闭**非池化连接——对「一条连接跑多个事务」的仓储用法是错的。故这里
    手动 commit/rollback，把连接生命周期交还调用方。yield 出连接本身，方便在同一
    事务内取 cursor。
    """
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _applied_filenames(conn):
    """读取 schema_migrations 已记录的文件名集合。"""
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def ensure_schema(conn):
    """按文件名序执行 migrations/*.sql，幂等。

    步骤：
      1. 确保 schema_migrations 记录表存在并提交；
      2. 读取已应用文件名集合；
      3. 对每个尚未应用的 .sql（按文件名升序）：执行其内容，并写入记录，提交。

    重复调用安全：已应用的脚本跳过；脚本内部亦应 ``IF NOT EXISTS`` 自保。
    返回本次扫描到的全部迁移文件名列表（已排序）。
    """
    if not _MIGRATIONS_DIR.is_dir():
        raise RuntimeError(
            "迁移目录不存在：%s" % _MIGRATIONS_DIR
        )

    # 1. 记录表
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_MIGRATIONS_DDL)
    conn.commit()

    # 2. 已应用集合
    applied = _applied_filenames(conn)

    # 3. 逐个应用
    files = sorted(p.name for p in _MIGRATIONS_DIR.glob("*.sql"))
    for fname in files:
        if fname in applied:
            continue
        sql = (_MIGRATIONS_DIR / fname).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT (filename) DO NOTHING",
                (fname,),
            )
        conn.commit()

    return files


def migrations_dir():
    """返回迁移脚本目录路径（供测试/工具检视）。"""
    return _MIGRATIONS_DIR
