# -*- coding: utf-8 -*-
"""建号/兑换授权面契约（R3 Wave1-Money 单轨；文件名沿用历史名）。

背景契约（钉死）：原 ``platform_settings.user_spend_target`` 双轨已拆除——
role=user 的授权面**恒**为一次性总额度（ai_spend_total_allowances），demo=
每周窗口、owner=每月窗口不受影响：

- 建号（user_store_pg.create_user_with_total_allowance）与登录惰性激活
  （registration_store.lazy_activate_pending_user，2026-10-08 §4 起替代
  已退役的邀请码兑换）**必须**同事务建 allowance 行：
  显式 X 按面值建行（default_version=None）；无 X 解析全局默认
  （**只查** ai_spend_total_defaults，default_version=默认行版本）；
  defaults 缺行 → ValueError ``total_default_missing``（路由层 400；
  建号/兑换整体回滚），绝不建出无额度行的用户（缺行 = 授权面永久
  fail-closed 的数据损坏）；
- 不再有 window 分叉/过渡 override：无论历史上有多少旧窗口/override 行，
  新建号一律只落 allowance 面。

覆盖矩阵（store 级；端点层见 test_admin_batch_d.py）：

- create_user_with_total_allowance：显式 X / defaults 行解析 / 缺默认
  fail-closed（用户零残留）；
- create_user（role=user 委托）：无 X 走默认建行；
- 兑换（redeem_invite）已随邀请码注册退役删除；「默认解析/缺默认回滚」
  由惰性激活承担（test_spend_total_allowances.py）。

运行：python3 -m pytest tests/test_user_creation_spend_target.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import pytest  # noqa: E402

import spend_store  # noqa: E402
import user_store  # noqa: E402
import user_store_pg  # noqa: E402

import _billing_helpers as bh  # noqa: E402


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
def _defaults_version():
    """读 defaults 行当前 version（行必在——seed_single_track_baseline 已种）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM ai_spend_total_defaults "
                        "WHERE singleton='global'")
            return int(cur.fetchone()["version"])
    finally:
        conn.close()


def _clear_defaults():
    """构造「defaults 缺行」场景（total_default_missing fail-closed 用）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_spend_total_defaults")
        conn.commit()
    finally:
        conn.close()


def _override_rows(user_id=None):
    """user_override 策略行数（可按 user_id 过滤；单轨后恒 0——写 API 已删）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            if user_id is None:
                cur.execute("SELECT count(*)::int AS n FROM ai_spend_policies "
                            "WHERE scope_type='user_override'")
            else:
                cur.execute("SELECT count(*)::int AS n FROM ai_spend_policies "
                            "WHERE scope_type='user_override' AND scope_id=%s",
                            (user_id,))
            return int(cur.fetchone()["n"])
    finally:
        conn.close()


def _allowance_row(user_id):
    """ai_spend_total_allowances 原始行（不存在返回 None）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ai_spend_total_allowances "
                        "WHERE subject_id=%s", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def _user_create_audit_detail(user_id):
    """user.create 审计 detail（同事务审计；缺失返回 None）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT detail FROM audit_events "
                        "WHERE action='user.create' AND target_id=%s",
                        (user_id,))
            row = cur.fetchone()
            return dict(row["detail"]) if row else None
    finally:
        conn.close()


def _mk_owner(login="sp-target-owner@x.com"):
    return user_store.create_user(login, "ownerpass123456", role="owner")


# --------------------------------------------------------------------------- #
# 1. 建号恒建 allowance（user_store_pg.create_user_with_total_allowance）
# --------------------------------------------------------------------------- #
def test_explicit_limit_creates_allowance():
    """显式 X → 同事务建 allowance 行（source=admin_create，
    default_version=None——面值是显式决策，不锚定任何默认版本）。"""
    bh.seed_spend_settings()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "total-x@x.com", "password-123456",
        total_limit_nano_cny=12 * 10 ** 9)
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 12 * 10 ** 9
    assert allowance["source"] == "admin_create"
    assert allowance["default_version"] is None
    row = _allowance_row(user["user_id"])
    assert row is not None
    assert int(row["limit_nano_cny"]) == 12 * 10 ** 9
    # 单轨：不建任何 user_override（写 API 已删，历史语义载体不出现）
    assert _override_rows(user["user_id"]) == 0
    # 审计 detail：目标/生效面值/面形态（非敏感字段；spend_target 恒标注）
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["spend_target"] == "total_allowance"
    assert detail["total_limit_nano_cny"] == 12 * 10 ** 9
    assert detail["effective_limit_nano_cny"] == 12 * 10 ** 9
    assert detail["limit_surface"] == "total_allowance"


def test_no_limit_resolves_total_defaults_row():
    """无 X + ai_spend_total_defaults 有行 → allowance=默认面值（0032 物化
    0023 user_default = 20 CNY），default_version=默认行版本。"""
    bh.seed_spend_settings()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "total-default@x.com", "password-123456")
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 20 * 10 ** 9
    assert allowance["source"] == "admin_create"
    assert allowance["default_version"] == _defaults_version()
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["effective_limit_nano_cny"] == 20 * 10 ** 9
    assert detail["limit_surface"] == "total_allowance"


def test_no_limit_ignores_user_default_policy_without_defaults_row():
    """defaults 缺行时**不**回退 user_default 策略面值（兼容分支已删）——
    即使有效策略在场也拒绝：唯一事实源是 defaults 表。"""
    bh.seed_spend_settings()
    _clear_defaults()
    policy = spend_store.resolve_policy("user", "__no_such_user__")
    assert policy is not None and policy["scope_type"] == "user_default"
    with pytest.raises(ValueError) as exc_info:
        user_store_pg.create_user_with_total_allowance(
            "no-fallback@x.com", "password-123456")
    assert "total_default_missing" in str(exc_info.value)
    assert user_store.get_user_by_login_id("no-fallback@x.com") is None


def test_without_any_default_rejected_and_rolled_back():
    """无 X + defaults 缺行 → ValueError total_default_missing；整体回滚
    （用户不存在、无半创建行）。绝不建出无额度行的用户（授权面永久
    fail-closed 红线）。"""
    bh.seed_spend_settings()
    _clear_defaults()
    with pytest.raises(ValueError) as exc_info:
        user_store_pg.create_user_with_total_allowance(
            "no-default@x.com", "password-123456")
    assert "total_default_missing" in str(exc_info.value)
    assert user_store.get_user_by_login_id("no-default@x.com") is None
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int AS n FROM "
                        "ai_spend_total_allowances")
            assert cur.fetchone()["n"] == 0
    finally:
        conn.close()


def test_create_user_delegate_always_builds_allowance():
    """create_user（role=user 委托）同样恒建行：无 X 走 defaults 面值；
    旧名 create_user_with_spend_override 已删除（兼容壳随单轨退役）。"""
    bh.seed_spend_settings()
    user = user_store.create_user("delegated@x.com", "password-123456")
    row = _allowance_row(user["user_id"])
    assert row is not None
    assert int(row["limit_nano_cny"]) == 20 * 10 ** 9
    assert not hasattr(user_store_pg, "create_user_with_spend_override")


# --------------------------------------------------------------------------- #
# 2. 兑换（redeem_invite）已随邀请码注册退役删除（2026-10-08 §4）：
#    「无面值解析默认 / 缺默认 fail-closed 整体回滚」的同款语义由登录惰性
#    激活（lazy_activate_pending_user）承担，见 test_spend_total_allowances
#    .test_lazy_activation_creates_allowance_atomically /
#    test_lazy_activation_missing_default_fails_closed。
# --------------------------------------------------------------------------- #
