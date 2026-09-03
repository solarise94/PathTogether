# -*- coding: utf-8 -*-
"""cutover 契约：``user_spend_target`` 驱动的建号/兑换分叉测试（review 修复）。

背景契约（钉死）：``platform_settings.user_spend_target`` ∈ {"window",
"total_allowance"}（spend_store.get_user_spend_target_tx 事务内读取，缺键/
非法值回退 "window"；cutover 前线上为 "window"）：

- target=``total_allowance``：每个 role=user 用户**必须**有
  ai_spend_total_allowances 行——建号/兑换同事务建行（显式 X 或解析默认，
  default_version 固化默认版本）；两者皆无 → ValueError
  ``total_default_missing``（建号整体回滚 / 兑换整体回滚），绝不建出无额度
  行的用户（否则授权面永久 fail-closed）；
- target=``window``（cutover 前）：建号/兑换**不得**建 allowance 行
  （dormant 行形成双真相，且 cutover apply 会因限额不一致 abort）；显式 X
  同步建等值 ``user_override`` 过渡策略（calendar_month），如实展示现行
  授权面。

覆盖矩阵（store 级；端点层见 test_admin_batch_d.py）：

- user_store_pg.create_user_with_total_allowance：两模式 × 显式 X /
  默认解析（defaults 行、user_default 策略回退）/ 缺默认 fail-closed；
- registration_store.redeem_invite：total 模式无 X 走默认建行（source=
  invite）、缺默认拒绝且回滚、window 模式带 X 建 override 不建行。

运行：RUN_PG_TESTS=1 python3 -m pytest tests/test_user_creation_spend_target.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
import pytest  # noqa: E402

import registration_store  # noqa: E402
import spend_store  # noqa: E402
import user_store  # noqa: E402
import user_store_pg  # noqa: E402
from pg_compat import BACKEND  # noqa: E402

PG = pytest.mark.skipif(BACKEND != "postgres",
                        reason="spend/invite 写路径需 PG（RUN_PG_TESTS=1）")

if BACKEND == "postgres":
    import _billing_helpers as bh  # noqa: E402


# --------------------------------------------------------------------------- #
# 公共基建
# --------------------------------------------------------------------------- #
def _seed_total_target():
    """0029 种子（target/settings 键）+ 直接置 total_allowance（测试固定前置态）。"""
    bh.seed_spend_policies()
    bh.seed_spend_settings()
    bh.set_user_spend_target("total_allowance")


def _disable_user_default_policy():
    """禁用 spp_user_default（构造「defaults 缺行且 user_default 策略也没有」
    的 fail-closed 场景；0023 种子默认 enabled）。"""
    conn = bh.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_spend_policies SET enabled=false "
                        "WHERE policy_id='spp_user_default'")
        conn.commit()
    finally:
        conn.close()


def _override_rows(user_id=None):
    """user_override 策略行数（可按 user_id 过滤）。"""
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
# 1. 建号分叉（user_store_pg.create_user_with_total_allowance）
# --------------------------------------------------------------------------- #
@PG
def test_total_target_explicit_limit_creates_allowance():
    """total 模式 + 显式 X → 同事务建 allowance 行（source=admin_create，
    default_version=None——面值是显式决策，不锚定任何默认版本）。"""
    _seed_total_target()
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
    # window 语义载体不出现：total 模式不建 user_override
    assert _override_rows(user["user_id"]) == 0
    # 审计 detail：目标/生效面值/面形态（非敏感字段）
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["spend_target"] == "total_allowance"
    assert detail["total_limit_nano_cny"] == 12 * 10 ** 9
    assert detail["effective_limit_nano_cny"] == 12 * 10 ** 9
    assert detail["limit_surface"] == "total_allowance"


@PG
def test_total_target_no_limit_resolves_total_defaults_row():
    """total 模式 + 无 X + ai_spend_total_defaults 有行 → allowance=默认面值，
    default_version=默认行版本。"""
    _seed_total_target()
    spend_store.set_total_default(30 * 10 ** 9, 1, updated_by="pytest")
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "total-default@x.com", "password-123456")
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 30 * 10 ** 9
    assert allowance["source"] == "admin_create"
    assert allowance["default_version"] == 1
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["effective_limit_nano_cny"] == 30 * 10 ** 9
    assert detail["limit_surface"] == "total_allowance"


@PG
def test_total_target_no_limit_falls_back_to_user_default_policy():
    """total 模式 + 无 X + defaults 缺行 → 回退 user_default 策略面值
    （0023 种子 20 CNY），default_version=该策略版本。"""
    _seed_total_target()
    policy = spend_store.resolve_policy("user", "__no_such_user__")
    assert policy is not None and policy["scope_type"] == "user_default"
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "total-fallback@x.com", "password-123456")
    assert allowance is not None
    assert allowance["limit_nano_cny"] == 20 * 10 ** 9
    assert allowance["default_version"] == policy["version"]


@PG
def test_total_target_without_any_default_rejected_and_rolled_back():
    """total 模式 + 无 X + defaults 缺行 + user_default 策略被禁 →
    ValueError total_default_missing；整体回滚（用户不存在、无半创建行）。
    绝不建出无额度行的用户（授权面永久 fail-closed 红线）。"""
    _seed_total_target()
    _disable_user_default_policy()
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


@PG
def test_window_target_explicit_limit_creates_override_not_allowance():
    """window 模式（cutover 前）+ 显式 X → **不建** allowance 行；建等值
    user_override 过渡策略（calendar_month），如实展示现行授权面。"""
    bh.seed_spend_policies()   # target 不 seed = window（缺省回退）
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "window-x@x.com", "password-123456",
        total_limit_nano_cny=7 * 10 ** 9)
    assert allowance is None
    assert spend_store.get_total_allowance(user["user_id"]) is None
    assert _allowance_row(user["user_id"]) is None
    policy = spend_store.resolve_policy("user", user["user_id"])
    assert policy is not None
    assert policy["scope_type"] == "user_override"
    assert policy["period_kind"] == "calendar_month"
    assert policy["limit_nano_cny"] == 7 * 10 ** 9
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["spend_target"] == "window"
    assert detail["total_limit_nano_cny"] is None      # allowance 面值（无行）
    assert detail["effective_limit_nano_cny"] == 7 * 10 ** 9
    assert detail["limit_surface"] == "user_override"


@PG
def test_window_target_without_limit_creates_no_spend_surface():
    """window 模式 + 无 X → 无 allowance、无 override（不造 dormant 行）。"""
    bh.seed_spend_policies()
    user, allowance = user_store_pg.create_user_with_total_allowance(
        "window-plain@x.com", "password-123456")
    assert allowance is None
    assert spend_store.get_total_allowance(user["user_id"]) is None
    assert _override_rows() == 0
    detail = _user_create_audit_detail(user["user_id"])
    assert detail["spend_target"] == "window"
    assert detail["effective_limit_nano_cny"] is None
    assert detail["limit_surface"] == "default_window_policy"


# --------------------------------------------------------------------------- #
# 2. 兑换分叉（registration_store.redeem_invite）
# --------------------------------------------------------------------------- #
@PG
def test_redeem_total_target_without_limit_resolves_default():
    """兑换 total 模式无模板面值 → 解析默认建行（source=invite，
    default_version=默认版本；旧 monthly 列兼容读取保留在显式面值路径）。"""
    _seed_total_target()
    spend_store.set_total_default(25 * 10 ** 9, 1, updated_by="pytest")
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="r-default@x.com")
    out = registration_store.redeem_invite(inv["token"], "r-default@x.com",
                                           "password-123456")
    allowance = out["total_allowance"]
    assert allowance is not None
    assert allowance["source"] == "invite"
    assert allowance["limit_nano_cny"] == 25 * 10 ** 9
    assert allowance["default_version"] == 1
    assert out["spend_override_policy"] is None   # total 模式不建 override
    assert _override_rows() == 0


@PG
def test_redeem_total_target_without_any_default_fails_closed():
    """兑换 total 模式且无任何默认 → ValueError total_default_missing；
    整体回滚（用户不创建、邀请不消费）。"""
    _seed_total_target()
    _disable_user_default_policy()
    owner = _mk_owner()
    inv = registration_store.create_invite(owner["user_id"],
                                           login_id="r-miss@x.com")
    with pytest.raises(ValueError) as exc_info:
        registration_store.redeem_invite(inv["token"], "r-miss@x.com",
                                         "password-123456")
    assert "total_default_missing" in str(exc_info.value)
    assert user_store.get_user_by_login_id("r-miss@x.com") is None
    row = registration_store.get_invite(inv["invite_id"])
    assert row["consumed_at"] is None and row["use_count"] == 0


@PG
def test_redeem_window_target_with_limit_creates_override_not_allowance():
    """兑换 window 模式带模板面值 → 不建 allowance；建等值 user_override
    过渡策略；返回 spend_override_policy 如实带面值（原恒 None 占位复用）。"""
    bh.seed_spend_policies()
    owner = _mk_owner()
    inv = registration_store.create_invite(
        owner["user_id"], login_id="r-window@x.com",
        total_limit_nano_cny=9 * 10 ** 9)
    out = registration_store.redeem_invite(inv["token"], "r-window@x.com",
                                           "password-123456")
    assert out["total_allowance"] is None
    assert out["spend_override_policy"] == {"limit_nano_cny": 9 * 10 ** 9}
    uid = out["user"]["user_id"]
    assert spend_store.get_total_allowance(uid) is None
    policy = spend_store.resolve_policy("user", uid)
    assert policy is not None
    assert policy["scope_type"] == "user_override"
    assert policy["period_kind"] == "calendar_month"
    assert policy["limit_nano_cny"] == 9 * 10 ** 9
