# -*- coding: utf-8 -*-
"""PUT /api/ai/config 权威校验测试。

覆盖：
  - 每个调优字段 / max_tokens 的负数、0（按语义）、非整数、超上限 → 400；
  - reserve_tokens + keep_recent_tokens >= context_window_tokens → 400；
  - 合法负载 200 且值正确落盘（含 0 表示"禁用/不保留"类字段）；
  - 非法负载不产生部分写入（校验失败后 ai_config.json 保持原值）。

隔离：独立临时 SHARE_DATA_DIR，绝不触碰 ~/svs-viewer/share-data 真实数据。
隔离方式：env 先于 import（保证 app._data_dir_for_secret 运行时读到本目录）+ 每
用例 autouse fixture 用 monkeypatch 夺回 env 与 share_store 常量 + 路径断言。
不在收集阶段 importlib.reload（避免收集期顺序依赖，见问题 1）。

运行：cd 项目根 && python3 -m pytest tests/test_ai_config_validation.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

# --------------------------------------------------------------------------- #
# 关键：SHARE_DATA_DIR 环境变量必须在 import share_store 之前设置（它是 share_store
# 模块级常量的初值来源）。但本模块绝不在收集阶段 importlib.reload —— reload 会把
# 共享的 share_store 模块对象改写到最后一个被收集模块的目录，制造收集期顺序依赖
# （见问题 1）。真正的路径隔离放进每个用例前的 autouse fixture，用 monkeypatch
# 夺回 share_store 常量与 env，用例结束自动还原。share_store 此时可能已被别的测试
# 模块预导入、其常量指向别人的目录，这在收集阶段是正常的，不在收集阶段断言。
# --------------------------------------------------------------------------- #
TMP = tempfile.mkdtemp(prefix="svs-aicfg-")
DATA_DIR = os.path.join(TMP, "share-data")
os.environ["SHARE_DATA_DIR"] = DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

import share_store  # noqa: E402

# openslide 未安装时 stub（本测试只覆盖配置校验，不需真 OpenSlide）
try:
    import openslide  # noqa: F401
except ImportError:
    import types as _types
    _os = _types.ModuleType("openslide")
    _os.OpenSlide = object
    sys.modules["openslide"] = _os
    _dz = _types.ModuleType("openslide.deepzoom")
    _dz.DeepZoomGenerator = object
    sys.modules["openslide.deepzoom"] = _dz

import app as app_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch):
    """每个用例前把 SHARE_DATA_DIR / share_store 常量指回本模块的临时目录。

    必要性：pytest 在收集阶段会 import 所有测试模块；其它测试模块（如
    test_ai_proxy）在 import 时会把 os.environ["SHARE_DATA_DIR"] 改成它们自己的
    临时目录。app._data_dir_for_secret() 在调用时（非 import 时）读取该环境变量，
    若不在此处夺回，配置路径会落到别的测试的临时目录里。同理 share_store.SHARE_FILE
    可能被别的模块的 fixture 改写到其临时目录，故一并夺回。monkeypatch 保证用例
    结束后还原 env 与常量，互不污染。

    绝不在收集阶段 importlib.reload share_store —— reload 会把共享的 share_store
    模块对象改写到最后一个被收集模块的目录，制造收集期顺序依赖（见问题 1）。
    """
    data_dir = Path(DATA_DIR)
    share_file = data_dir / "shares.json"
    monkeypatch.setenv("SHARE_DATA_DIR", DATA_DIR)
    monkeypatch.setattr(share_store, "SHARE_DATA_DIR", data_dir)
    monkeypatch.setattr(share_store, "SHARE_FILE", share_file)
    # 夺回后立刻校验落点在本次临时目录内（失败即中断，避免误写真实数据）。
    p = app_mod._ai_config_path()
    assert str(p).startswith(TMP), (
        "_ai_config_path() 未隔离到临时目录！期望前缀 %r，实际 %r" % (TMP, str(p)))
    assert str(share_store.SHARE_FILE).startswith(TMP), (
        "share_store.SHARE_FILE 未隔离到临时目录！期望前缀 %r，实际 %r"
        % (TMP, str(share_store.SHARE_FILE)))
    yield

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))


def make_client():
    """Flask 测试客户端（认证关闭）。"""
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return app_mod.app.test_client()


def reset_config():
    """每个用例前清空 ai_config.json（落在临时目录内）。"""
    p = app_mod._ai_config_path()
    assert str(p).startswith(TMP), (
        "reset_config 目标路径越界！%r 不在临时目录 %r 内" % (str(p), TMP))
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def seed_config(**fields):
    """直接写一份已知合法配置（绕过端点），作为"部分写入"检测的基线。"""
    app_mod._save_ai_config(dict(fields))


def load_raw():
    """读取磁盘 ai_config.json 原文（dict），用于断言落盘结果。"""
    p = app_mod._ai_config_path()
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def put(body):
    """发起 PUT /api/ai/config，返回 (status_code, json_body)。"""
    client = make_client()
    r = client.put("/api/ai/config",
                   data=json.dumps(body),
                   content_type="application/json")
    try:
        j = r.get_json()
    except Exception:
        j = None
    return r.status_code, j


# =========================================================================== #
# 正整数类字段：context_window_tokens / max_steps / fork_active_limit /
# lease_ttl / max_tokens / event_buffer —— 拒绝 <=0、负数、非整数。
# =========================================================================== #
def test_positive_int_fields_reject_negative():
    print("== 正整数类字段：负数 → 400 ==")
    cases = {
        "context_window_tokens": -1,
        "max_steps": -2,
        "fork_active_limit": -4,
        "lease_ttl": -5,
        "max_tokens": -1,
        "event_buffer": -1,
    }
    for field, val in cases.items():
        reset_config()
        code, j = put({field: val})
        check("%s=%r → 400" % (field, val), code == 400,
              "got %s %r" % (code, j))
        check("%s=负数 error 含字段名" % field,
              j and isinstance(j.get("error"), str) and field in j["error"],
              "error=%r" % (j or {}).get("error"))
        # 负数不得落盘
        check("%s=负数不落盘" % field, field not in load_raw())


def test_positive_int_fields_reject_zero():
    print("== 正整数类字段：0 → 400（0 非合法）==")
    for field in ("context_window_tokens", "max_steps", "fork_active_limit",
                  "lease_ttl", "max_tokens", "event_buffer"):
        reset_config()
        code, j = put({field: 0})
        check("%s=0 → 400" % field, code == 400, "got %s %r" % (code, j))


def test_positive_int_fields_reject_non_integer():
    print("== 正整数类字段：非整数 → 400 ==")
    for field in ("context_window_tokens", "max_steps", "fork_active_limit",
                  "lease_ttl", "max_tokens", "event_buffer"):
        reset_config()
        code, j = put({field: 12.5})
        check("%s=12.5 → 400" % field, code == 400, "got %s %r" % (code, j))
        # 字符串非数字
        reset_config()
        code, j = put({field: "abc"})
        check("%s='abc' → 400" % field, code == 400, "got %s %r" % (code, j))
        # None
        reset_config()
        code, j = put({field: None})
        check("%s=None → 400" % field, code == 400, "got %s %r" % (code, j))


# =========================================================================== #
# 非负整数类字段（0 允许）：reserve_tokens / keep_recent_tokens /
# keep_recent_images / safety_margin —— 允许 0，拒绝负数 / 非整数。
# =========================================================================== #
def test_nonneg_int_fields_allow_zero():
    print("== 非负整数类字段：0 → 200 落盘 ==")
    for field in ("reserve_tokens", "keep_recent_tokens", "keep_recent_images"):
        # 单独 0 不触发关系校验（context_window 未变，默认 272000，0+0 < 272000）
        reset_config()
        code, j = put({field: 0})
        check("%s=0 → 200" % field, code == 200, "got %s %r" % (code, j))
        if code == 200:
            check("%s=0 落盘为 0" % field, j.get(field) == 0,
                  "got %r" % j.get(field))


def test_safety_margin_deprecated_not_persisted():
    """safety_margin 已弃用（§11）：接受（校验通过）但不再写回或展示。"""
    print("== safety_margin 弃用：接受但不落盘 ==")
    reset_config()
    code, j = put({"safety_margin": 4096})
    check("safety_margin=4096 → 200（接受）", code == 200, "got %s %r" % (code, j))
    # 不落盘：磁盘上不应出现 safety_margin=4096（保留默认或既有值）
    raw = load_raw()
    check("safety_margin 不落盘为 4096", raw.get("safety_margin") != 4096,
          "got %r" % raw.get("safety_margin"))
    # 负数仍应被校验拒绝（接受的前提是通过校验）
    reset_config()
    code, j = put({"safety_margin": -3})
    check("safety_margin=-3 → 400（校验仍生效）", code == 400, "got %s %r" % (code, j))


def test_nonneg_int_fields_reject_negative():
    print("== 非负整数类字段：负数 → 400 ==")
    for field in ("reserve_tokens", "keep_recent_tokens", "keep_recent_images"):
        reset_config()
        code, j = put({field: -3})
        check("%s=-3 → 400" % field, code == 400, "got %s %r" % (code, j))
        check("%s=负数 error 含字段名" % field,
              j and field in (j or {}).get("error", ""),
              "error=%r" % (j or {}).get("error"))


# =========================================================================== #
# max_steps 上限：UI 声明 max=500。
# =========================================================================== #
def test_max_steps_upper_bound():
    print("== max_steps 超上限 → 400 ==")
    reset_config()
    code, j = put({"max_steps": 99999})  # 任务背景里的失控值
    check("max_steps=99999 → 400", code == 400, "got %s %r" % (code, j))
    check("error 提示上限", j and "max_steps" in (j or {}).get("error", ""),
          "error=%r" % (j or {}).get("error"))
    check("99999 不落盘", "max_steps" not in load_raw())
    # 边界：500 合法
    reset_config()
    code, j = put({"max_steps": 500})
    check("max_steps=500 → 200", code == 200, "got %s %r" % (code, j))
    check("max_steps=500 落盘", j and j.get("max_steps") == 500,
          "got %r" % (j or {}).get("max_steps"))
    # 边界：501 非法
    reset_config()
    code, j = put({"max_steps": 501})
    check("max_steps=501 → 400", code == 400, "got %s %r" % (code, j))


# =========================================================================== #
# 字段关系：reserve_tokens + keep_recent_tokens < context_window_tokens
# =========================================================================== #
def test_relationship_violation():
    print("== reserve + keep_recent >= context_window → 400 ==")
    # 三者同时提交，reserve+keep_recent == context_window（边界，违反 < ）
    reset_config()
    code, j = put({
        "context_window_tokens": 10000,
        "reserve_tokens": 5000,
        "keep_recent_tokens": 5000,  # 5000+5000 == 10000，违反严格小于
    })
    check("reserve+keep==ctx → 400", code == 400, "got %s %r" % (code, j))
    check("error 提示关系", j and "context_window_tokens" in (j or {}).get("error", ""),
          "error=%r" % (j or {}).get("error"))
    # 超出
    reset_config()
    code, j = put({
        "context_window_tokens": 10000,
        "reserve_tokens": 6000,
        "keep_recent_tokens": 6000,  # > 10000
    })
    check("reserve+keep>ctx → 400", code == 400, "got %s %r" % (code, j))

    # 合法：reserve+keep_recent < ctx
    reset_config()
    code, j = put({
        "context_window_tokens": 10000,
        "reserve_tokens": 4000,
        "keep_recent_tokens": 5000,  # 9000 < 10000
    })
    check("reserve+keep<ctx → 200", code == 200, "got %s %r" % (code, j))
    if code == 200:
        check("三者正确落盘",
              j.get("context_window_tokens") == 10000
              and j.get("reserve_tokens") == 4000
              and j.get("keep_recent_tokens") == 5000,
              "got ctx=%r reserve=%r keep=%r"
              % (j.get("context_window_tokens"), j.get("reserve_tokens"),
                 j.get("keep_recent_tokens")))


def test_relationship_uses_existing_values_when_partial():
    """只提交 reserve_tokens，但已落盘的 context_window/keep_recent 使关系违反。"""
    print("== 关系校验用既有值（部分提交也应触发）==")
    reset_config()
    # 先落一份合法基线
    seed_config(context_window_tokens=10000, reserve_tokens=1000,
                keep_recent_tokens=1000)
    # 单独把 reserve_tokens 提到 9500：9500+1000=10500 > 10000 → 400
    code, j = put({"reserve_tokens": 9500})
    check("部分提交使关系违反 → 400", code == 400, "got %s %r" % (code, j))


# =========================================================================== #
# 合法负载 200 且正确落盘（向后兼容：合法行为完全不变）
# =========================================================================== #
def test_legal_payload_persists():
    print("== 合法负载 200 且正确落盘 ==")
    reset_config()
    payload = {
        "base_url": "http://llm.example/v1",
        "model": "gpt-test",
        "max_tokens": 4096,
        "max_steps": 30,
        "context_window_tokens": 200000,
        "reserve_tokens": 8000,
        "safety_margin": 4096,
        "keep_recent_tokens": 10000,
        "keep_recent_images": 8,
        "fork_active_limit": 15,
        "lease_ttl": 120,
        "event_buffer": 150,
        "api_protocol": "openai",
    }
    code, j = put(payload)
    check("合法负载 → 200", code == 200, "got %s %r" % (code, j))
    for k, v in payload.items():
        check("落盘 %s=%r" % (k, v), j.get(k) == v,
              "got %r" % j.get(k))
    # 磁盘也落对（max_tokens 是基础字段）
    raw = load_raw()
    check("磁盘 max_steps=30", raw.get("max_steps") == 30)
    check("磁盘 max_tokens=4096", raw.get("max_tokens") == 4096)
    check("磁盘 lease_ttl=120", raw.get("lease_ttl") == 120)


def test_float_with_integer_value_accepted():
    """整数值的 float（如 16000.0）应被接受并归一为 int（向后兼容）。"""
    print("== 整数值 float → 200（归一为 int）==")
    reset_config()
    code, j = put({"reserve_tokens": 16000.0})
    check("reserve_tokens=16000.0 → 200", code == 200, "got %s %r" % (code, j))
    check("归一为 int 16000", j.get("reserve_tokens") == 16000,
          "got %r (%s)" % (j.get("reserve_tokens"),
                           type(j.get("reserve_tokens")).__name__))


def test_numeric_string_accepted():
    """数字串（前端可能传 "16000"）应被接受（向后兼容）。"""
    print("== 数字串 → 200 ==")
    reset_config()
    code, j = put({"fork_active_limit": "25"})
    check("fork_active_limit='25' → 200", code == 200, "got %s %r" % (code, j))
    check("归一为 int 25", j.get("fork_active_limit") == 25,
          "got %r" % j.get("fork_active_limit"))


def test_omitted_fields_keep_defaults():
    """未提交的字段保持缺省回填（向后兼容）。"""
    print("== 未提交字段保持默认 ==")
    reset_config()
    code, j = put({"base_url": "http://x/v1"})
    check("只提交 base_url → 200", code == 200, "got %s %r" % (code, j))
    check("max_steps 回填默认 50", j.get("max_steps") == 50,
          "got %r" % j.get("max_steps"))
    check("context_window_tokens 回填默认 272000",
          j.get("context_window_tokens") == 272000,
          "got %r" % j.get("context_window_tokens"))


# =========================================================================== #
# 非法负载不产生部分写入（原子性）：校验失败后磁盘保持原值
# =========================================================================== #
def test_no_partial_write_on_failure():
    print("== 非法负载不产生部分写入（原子性）==")
    reset_config()
    baseline = {
        "base_url": "http://keep.example/v1",
        "model": "keep-model",
        "max_tokens": 2048,
        "max_steps": 40,
        "context_window_tokens": 200000,
        "reserve_tokens": 8000,
        "keep_recent_tokens": 10000,
    }
    seed_config(**baseline)
    snap_before = load_raw()

    # 混合负载：合法字段 + 一个非法字段（max_steps=99999）
    code, j = put({
        "base_url": "http://EVIL.example/v1",   # 合法但不应落盘
        "model": "evil-model",                  # 合法但不应落盘
        "max_steps": 99999,                     # 非法 → 整体拒绝
        "max_tokens": 8192,                     # 合法但不应落盘
    })
    check("混合非法负载 → 400", code == 400, "got %s %r" % (code, j))
    snap_after = load_raw()
    # 基线字段全部保持原值（未被合法字段覆盖，也未被非法字段污染）
    check("base_url 保持原值", snap_after.get("base_url") == baseline["base_url"],
          "got %r" % snap_after.get("base_url"))
    check("model 保持原值", snap_after.get("model") == baseline["model"],
          "got %r" % snap_after.get("model"))
    check("max_steps 保持原值 40", snap_after.get("max_steps") == 40,
          "got %r" % snap_after.get("max_steps"))
    check("max_tokens 保持原值 2048", snap_after.get("max_tokens") == 2048,
          "got %r" % snap_after.get("max_tokens"))
    # 整体结构不变（键集合一致）
    check("键集合未变", set(snap_after.keys()) == set(snap_before.keys()),
          "before=%r after=%r" % (sorted(snap_before), sorted(snap_after)))


def test_no_partial_write_on_relationship_failure():
    """关系校验失败也不部分写入（单字段已过、关系未过）。"""
    print("== 关系校验失败也不部分写入 ==")
    reset_config()
    seed_config(context_window_tokens=200000, reserve_tokens=8000,
                keep_recent_tokens=10000, max_steps=40)
    # max_steps=30 合法单字段，但 reserve+keep_recent 使关系违反
    code, j = put({
        "max_steps": 30,                 # 单字段合法
        "context_window_tokens": 10000,  # 使 8000+10000=18000 > 10000 违反
    })
    check("关系违反 → 400", code == 400, "got %s %r" % (code, j))
    snap = load_raw()
    check("max_steps 仍为原值 40（未部分写入）", snap.get("max_steps") == 40,
          "got %r" % snap.get("max_steps"))
    check("context_window 仍为原值 200000", snap.get("context_window_tokens") == 200000,
          "got %r" % snap.get("context_window_tokens"))


if __name__ == "__main__":
    test_positive_int_fields_reject_negative()
    test_positive_int_fields_reject_zero()
    test_positive_int_fields_reject_non_integer()
    test_nonneg_int_fields_allow_zero()
    test_nonneg_int_fields_reject_negative()
    test_safety_margin_deprecated_not_persisted()
    test_max_steps_upper_bound()
    test_relationship_violation()
    test_relationship_uses_existing_values_when_partial()
    test_legal_payload_persists()
    test_float_with_integer_value_accepted()
    test_numeric_string_accepted()
    test_omitted_fields_keep_defaults()
    test_no_partial_write_on_failure()
    test_no_partial_write_on_relationship_failure()
    print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
