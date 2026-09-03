# -*- coding: utf-8 -*-
"""Dispatcher 拆分测试（R3 Wave 3：默认即 PostgreSQL）。

覆盖：
  1. 默认后端为 postgres：create_share/list_shares 与 create_user/verify_user 冒烟
    走通 PG 实现（经 dispatcher，不经任何调用方改动）；
  2. STORAGE_BACKEND 非法值在 import 期报错；
  3. dispatcher 公共名与 json impl 源码声明的公共名集合一致（防漏 export）；
  4. PG 后端公共名接线（re-export，不再抛 RuntimeError）；
  5. PG 后端不得拉入 json 实现（share_store_pg 源码级断言）。

隔离：独立临时 SHARE_DATA_DIR，绝不触碰真实数据。dispatcher 读 STORAGE_BACKEND 时机
在 import 期，故 postgres/非法值场景用 importlib 加载**全新**模块实例（独立模块名），
不污染进程内默认的 postgres dispatcher。
"""
import ast
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# 让 tests 能 import 仓库根目录下的模块
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# SHARE_DATA_DIR 必须在 import share_store 之前设置（模块级常量初值来源）
_TMP = tempfile.mkdtemp(prefix="svs-dispatch-")
os.environ.setdefault("SHARE_DATA_DIR", os.path.join(_TMP, "share-data"))
os.makedirs(os.environ["SHARE_DATA_DIR"], exist_ok=True)

import pytest  # noqa: E402

import share_store  # noqa: E402  默认 postgres dispatcher
import user_store  # noqa: E402
import share_store_json  # noqa: E402  json 实现（仅作 export 对比源）
import user_store_json  # noqa: E402

DISPATCHER_INFRA_NAMES = {"STORAGE_BACKEND"}  # dispatcher 自身额外暴露的非业务公共名


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _declared_public_names(source_path):
    """解析模块源码，返回顶层声明的公共名集合（非下划线开头）。

    用 AST 而非运行时 dir()：后者会混入标准库导入（json/os/pathlib.Path 等），
    无法干净区分「本模块定义的公共 API」与「顺手导入的名字」。
    """
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {n for n in names if not n.startswith("_")}


def _module_public_attrs(mod):
    """模块运行时实际暴露的公共属性名（__dict__ 键，非下划线开头）。"""
    return {n for n in vars(mod) if not n.startswith("_")}


def _load_fresh(source_path, mod_name):
    """以独立模块名加载一份全新的 dispatcher（读当前 os.environ 的 STORAGE_BACKEND）。

    加载完即从 sys.modules 移除，避免污染后续 import。异常向上抛（供非法值断言）。
    """
    spec = importlib.util.spec_from_file_location(mod_name, str(source_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    sys.modules.pop(mod_name, None)
    return mod


# --------------------------------------------------------------------------- #
# 1. 默认 postgres 后端冒烟（经 dispatcher）
# --------------------------------------------------------------------------- #
def test_default_backend_is_postgres():
    assert share_store.STORAGE_BACKEND == "postgres"
    assert user_store.STORAGE_BACKEND == "postgres"


def test_postgres_smoke_share_store():
    """create_share → list_shares 走通 postgres 实现（conftest 已起内嵌 PG）。"""
    token = share_store.create_share(slides=["demo.svs"], expires_hours=1)
    assert token and token.get("token")
    listed = share_store.list_shares()
    assert any(s["token"] == token["token"] for s in listed)


def test_postgres_smoke_user_store():
    """create_user → verify_user 走通 postgres 实现（密码满足批次 A 15..200 策略）。"""
    login_id = "alice@example.com"
    password = "Password12345678"  # 16 字符（≥ PASSWORD_MIN_LENGTH=15）
    created = user_store.create_user(login_id, password, role="user")
    assert created and created["login_id"] == login_id
    assert user_store.verify_user(login_id, password)  # dict（truthy）
    assert not user_store.verify_user(login_id, "wrong-password-xxx")


# --------------------------------------------------------------------------- #
# 2. STORAGE_BACKEND=postgres/dual 时公共名已接线（re-export，不再抛 RuntimeError）
#    active dispatcher 即 postgres；再以独立模块名加载验证 re-export 与非法值。
# --------------------------------------------------------------------------- #
import importlib.util as _ilu  # noqa: E402

_PSYCOPG_AVAILABLE = _ilu.find_spec("psycopg") is not None


@pytest.mark.skipif(
    not _PSYCOPG_AVAILABLE, reason="缺 psycopg：postgres 后端 import 需该依赖")
def test_postgres_backend_exports_public_names(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    mod = _load_fresh(_REPO_ROOT / "share_store.py", "ss_pg_test")
    # 公共名已 re-export 到模块 __dict__（不再是 __getattr__ 抛 RuntimeError）
    assert "create_share" in vars(mod)
    assert callable(mod.create_share)
    assert mod.STORAGE_BACKEND == "postgres"
    # 非业务名的缺失仍按正常 AttributeError 行为
    with pytest.raises(AttributeError):
        mod.this_does_not_exist


@pytest.mark.skipif(
    not _PSYCOPG_AVAILABLE, reason="缺 psycopg：postgres 后端 import 需该依赖")
def test_postgres_backend_user_store_exports(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "postgres")
    mod = _load_fresh(_REPO_ROOT / "user_store.py", "us_pg_test")
    assert "verify_user" in vars(mod)
    assert callable(mod.verify_user)


# --------------------------------------------------------------------------- #
# 3. dispatcher 公共名 == json impl 源码声明的公共名（防漏 export）
# --------------------------------------------------------------------------- #
def test_share_store_export_matches_json_impl():
    declared = _declared_public_names(_REPO_ROOT / "share_store_json.py")
    exposed = _module_public_attrs(share_store) - DISPATCHER_INFRA_NAMES
    assert exposed == declared, (
        "dispatcher 与 share_store_json 公共名不一致：\n"
        "  仅 dispatcher 有: %s\n  仅 json 实现有: %s"
        % (sorted(exposed - declared), sorted(declared - exposed))
    )
    # 显式计数码条
    assert len(declared) == len(share_store._JSON_PUBLIC_NAMES)
    assert set(share_store._JSON_PUBLIC_NAMES) == declared


def test_user_store_export_matches_json_impl():
    declared = _declared_public_names(_REPO_ROOT / "user_store_json.py")
    exposed = _module_public_attrs(user_store) - DISPATCHER_INFRA_NAMES
    assert exposed == declared, (
        "dispatcher 与 user_store_json 公共名不一致：\n"
        "  仅 dispatcher 有: %s\n  仅 json 实现有: %s"
        % (sorted(exposed - declared), sorted(declared - exposed))
    )
    assert len(declared) == len(user_store._JSON_PUBLIC_NAMES)
    assert set(user_store._JSON_PUBLIC_NAMES) == declared


def test_share_store_pg_has_no_json_import():
    """PG 后端不得再拉入 json 实现（r3 wave3 拆分验收）。

    源码级断言：share_store_pg.py 全文不得出现 `share_store_json`（import 语句、
    注释、docstring 一概不许），否则 import 期会执行 json 模块（mkdir
    SHARE_DATA_DIR 等 IO）。import 语句不可见（ImportFrom 不被 AST 计数），故
    用源码字符串精确匹配。
    """
    src = (_REPO_ROOT / "share_store_pg.py").read_text(encoding="utf-8")
    assert "share_store_json" not in src
    assert "from share_shared import" in src


# --------------------------------------------------------------------------- #
# 4. STORAGE_BACKEND 非法值 import 期报错
# --------------------------------------------------------------------------- #
def test_invalid_backend_raises_at_import(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "mysql")
    with pytest.raises(ValueError):
        _load_fresh(_REPO_ROOT / "share_store.py", "ss_bad_test")