# -*- coding: utf-8 -*-
"""新增测试：#4 ROI source 数据修正 / api_key 加密往返 / fork 💬 渲染条件。

运行：cd 项目根 && python3 tests/test_ai_fixes.py
用独立临时 SHARE_DATA_DIR，避免污染真实数据。

隔离策略（兼容 pytest 与脚本两种执行方式）：
- 收集/导入阶段：只设置本模块的 SHARE_DATA_DIR 环境变量并 import share_store
  （share_store 可能已被别的测试模块预导入，此时其模块常量 SHARE_DATA_DIR/
  SHARE_FILE 指向别人的临时目录，这是正常的，不在收集阶段断言）。
- pytest 运行：每个用例前的 autouse fixture 用 monkeypatch 把 share_store 的
  SHARE_DATA_DIR/SHARE_FILE 重新指回本模块的临时目录，并断言落点在本目录内。
  monkeypatch 自动还原，互不污染。绝不在收集阶段 importlib.reload —— reload 会
  把共享的 share_store 模块对象改写到最后一个被收集模块的目录，制造顺序依赖。
- 脚本运行（python3 tests/test_ai_fixes.py）：进程独占、无顺序问题，在 main 开头
  直接 setattr 即可（没有 monkeypatch fixture 可用）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
SHARE_DATA_DIR = _bootstrap.SHARE_DATA_DIR
TMP = _bootstrap.SESSION_TMP  # 「路径必须落在临时树内」防御断言用
import share_store  # noqa: E402
# check()：_pt_helpers 统一带守卫实现；PASS/FAIL 计数仍落在本模块
from _pt_helpers import check, isolate_app  # noqa: E402

# cryptography 可用性检测（影响 api_key 加密是否启用）
try:
    import cryptography  # noqa: F401
    HAS_CRYPTO = True
except Exception:
    HAS_CRYPTO = False

import pytest  # noqa: E402

def _apply_isolation():
    """把 share_store 的模块常量指回本模块的临时目录，并断言落点在其内。

    防御性断言：所有写/unlink 目标必须落在本次测试的临时目录内，绝不触碰真实数据
    文件。失败即立即中断，避免 reset_store() 误删真实数据。

    被 pytest autouse fixture（每个用例前）与脚本模式 main 开头共同调用。
    用 pathlib.Path 保持 share_store.SHARE_FILE 的类型（测试代码依赖
    .write_text()/.read_text()/.unlink()）。
    """
    data_dir = Path(SHARE_DATA_DIR)
    share_file = data_dir / "shares.json"
    share_store.SHARE_DATA_DIR = data_dir
    share_store.SHARE_FILE = share_file
    data_dir.mkdir(parents=True, exist_ok=True)
    assert str(share_store.SHARE_FILE).startswith(TMP), (
        "SHARE_FILE 未隔离到临时目录！期望前缀 %r，实际 %r"
        % (TMP, str(share_store.SHARE_FILE)))

@pytest.fixture(autouse=True)
def _isolate_share_store(monkeypatch):
    """每个用例前夺回 share_store 常量到本模块临时目录。

    必要性：pytest 在收集阶段会 import 所有测试模块；其它模块（如
    test_ai_config_validation）的隔离 fixture 会把 share_store.SHARE_FILE 改写到
    它们自己的临时目录。若不在用例前夺回，本模块的 reset_store() 会写到别人的
    目录里。monkeypatch 用例结束后自动还原，互不污染。
    通用隔离主体已收敛到 _pt_helpers.isolate_app（test-review P3-16）。
    """
    isolate_app(monkeypatch, SHARE_DATA_DIR)
    # 越界检查：目标必须在本模块临时目录内，否则 fail（绝不 unlink 真实文件）。
    assert str(share_store.SHARE_FILE).startswith(TMP), (
        "SHARE_FILE 未隔离到临时目录！期望前缀 %r，实际 %r"
        % (TMP, str(share_store.SHARE_FILE)))
    yield

PASS = 0
FAIL = 0

def reset_store():
    # unlink 前再校验一次目标路径在临时目录内（多重防线，防 SHARE_FILE 被外部改回真实路径）
    assert str(share_store.SHARE_FILE).startswith(TMP), (
        "reset_store 目标路径越界！%r 不在临时目录 %r 内" % (str(share_store.SHARE_FILE), TMP))
    os.makedirs(str(share_store.SHARE_DATA_DIR), exist_ok=True)
    share_store.SHARE_FILE.unlink(missing_ok=True)

# =========================================================================== #
# #4：ROI 迁移 source 数据修正（只改 AI 落标，不误伤人工标注）
# =========================================================================== #

# =========================================================================== #
# #2：api_key 加密往返（存→读解密一致；GET 掩码不明文；明文迁移）
# =========================================================================== #
def test_api_key_encryption():
    print("== test_api_key_encryption（#2 加密）==")
    reset_store()
    # 清掉可能残留的 ai_secret.key / ai_config.json（隔离）
    import app as app_mod
    for p in (app_mod._ai_config_path(), app_mod._ai_secret_path()):
        try:
            p.unlink()
        except Exception:
            pass

    plain = "sk-test-1234567890abcdef"

    # PUT 明文 → 磁盘应为密文（enc: 前缀），GET 掩码不明文
    app_mod._save_ai_config({"base_url": "http://x/v1", "api_key": plain, "model": "m"})
    raw = json.loads(app_mod._ai_config_path().read_text(encoding="utf-8"))
    if HAS_CRYPTO:
        check("磁盘 api_key 为密文（enc: 前缀）", raw.get("api_key", "").startswith("enc:"),
              "got %r" % raw.get("api_key"))
        check("磁盘不明文存 api_key", plain not in raw.get("api_key", ""))
    else:
        check("无 cryptography 时磁盘降级明文", raw.get("api_key") == plain)

    # 读回解密 = 原明文
    cfg = app_mod._load_ai_config()
    check("读回解密 api_key 与原值一致", cfg.get("api_key") == plain, "got %r" % cfg.get("api_key"))

    # GET 掩码不明文
    mask = app_mod._mask_api_key(cfg.get("api_key") or "")
    check("掩码不含完整明文", plain not in mask, "mask=%r" % mask)
    check("掩码非空", bool(mask))

    # 明文旧配置迁移：直接写明文进磁盘 → 读取自动加密重写
    if HAS_CRYPTO:
        app_mod._ai_config_path().write_text(json.dumps(
            {"base_url": "http://x/v1", "api_key": "legacy-plain-key-xyz", "model": "m"}),
            encoding="utf-8")
        cfg2 = app_mod._load_ai_config()
        check("明文旧配置读取解密一致", cfg2.get("api_key") == "legacy-plain-key-xyz",
              "got %r" % cfg2.get("api_key"))
        raw2 = json.loads(app_mod._ai_config_path().read_text(encoding="utf-8"))
        check("明文旧配置迁移为密文落盘", raw2.get("api_key", "").startswith("enc:"),
              "got %r" % raw2.get("api_key"))

    # 清空 api_key
    app_mod._save_ai_config({"base_url": "http://x/v1", "api_key": "", "model": "m"})
    cfg3 = app_mod._load_ai_config()
    check("清空 api_key 读回空", cfg3.get("api_key") == "", "got %r" % cfg3.get("api_key"))

# =========================================================================== #
# #2：api_protocol 字段往返（openai 默认 / anthropic 接受 / 非法拒绝）
# =========================================================================== #
def test_api_protocol():
    print("== test_api_protocol（#2 协议字段）==")
    reset_store()
    import app as app_mod
    for p in (app_mod._ai_config_path(), app_mod._ai_secret_path()):
        try:
            p.unlink()
        except Exception:
            pass
    app_mod._save_ai_config({"base_url": "http://x/v1", "api_key": "k", "model": "m",
                             "api_protocol": "anthropic"})
    cfg = app_mod._load_ai_config()
    check("api_protocol 存取往返", cfg.get("api_protocol") == "anthropic",
          "got %r" % cfg.get("api_protocol"))
    # GET 默认 openai（无配置时）
    app_mod._save_ai_config({})
    cfg2 = app_mod._load_ai_config()
    check("缺省 api_protocol 读回 None（GET 层默认 openai）", cfg2.get("api_protocol") is None)

# =========================================================================== #
# #4 前端 💬 渲染条件：source=ai 才渲染（断言数据契约，前端条件已锁定为 source==='ai'）
# =========================================================================== #
def test_fork_render_condition():
    print("== test_fork_render_condition（#4 💬 渲染条件数据契约）==")
    reset_store()
    # 落一条 AI 标注 + 一条人工标注
    share_store.add_roi(share_store.ADMIN_TOKEN, "a.svs", "可疑区域-浸润性病变",
                        note="浸润性生长", x=10, y=10, side_px=100, source="ai")
    share_store.add_roi(share_store.ADMIN_TOKEN, "a.svs", "管理员",
                        note="手画", x=20, y=20, side_px=100, source="human")
    by_slide = share_store.annotations_by_slide()
    items = []
    for groups in by_slide.values():
        for g in groups:
            items.extend(g.get("items") or [])
    ai = [it for it in items if it.get("source") == "ai"]
    human = [it for it in items if it.get("source") == "human"]
    check("AI 标注满足渲染条件（source=ai 且 annotation_id）",
          all(it.get("source") == "ai" and it.get("annotation_id") for it in ai))
    check("人工标注不满足 ai 条件",
          all(it.get("source") == "human" for it in human))
    check("AI/人工各 1 条", len(ai) == 1 and len(human) == 1,
          "ai=%d human=%d" % (len(ai), len(human)))

if __name__ == "__main__":
    # 脚本模式：进程独占，无 pytest fixture，也无收集期顺序问题。直接 setattr
    # 把 share_store 常量指回本模块临时目录（保证越界断言成立）。
    _apply_isolation()
    test_roi_source_fix()
    test_api_key_encryption()
    test_api_protocol()
    test_fork_render_condition()
    print("\nPASS=%d FAIL=%d" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
