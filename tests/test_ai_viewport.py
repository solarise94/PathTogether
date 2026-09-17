# -*- coding: utf-8 -*-
"""P1「普通发送绑定浏览器当前视野」——平台代理 viewport 校验/透传测试。

覆盖 /api/ai/run、/api/ai/continue、/api/ai/branch、/api/ai/ask 四端点：
  - 合法 viewport（level-0 bbox {x,y,w,h}）→ 原样透传 sidecar payload
    （与 task/session_id 同级），审计 detail 记 bbox 四个数；
  - 非法形状（非 dict / 缺键 / 负尺寸 / 零尺寸 / 非数值 / 负坐标 / 超量级）
    → 400 invalid_argument，且**不**转发 sidecar（零副作用拒绝）；
  - 不带 viewport → 行为与现状完全一致（旧 UI 兼容）；
  - fork-ask（lite、无工具）不透传 viewport（本切片有界范围）。

方案沿用 test_ai_proxy.py：FakeRequests 替换 app.requests，无需起真 server。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: E402,F401  # session 目录+openslide stub（conftest 先行）
UPLOAD_DIR = _bootstrap.UPLOAD_DIR

import pytest  # noqa: E402

import app as app_mod  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, FakeRequests, FakeResponse  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
        return
    FAIL += 1
    print("FAIL  %s  %s" % (name, detail))
    if "PYTEST_CURRENT_TEST" in os.environ:
        raise AssertionError("FAIL %s %s" % (name, detail))


def install_fake_requests():
    fake = FakeRequests()
    app_mod.requests = fake
    return fake


def make_client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = False
    return csrf_client(app_mod.app.test_client())


def setup_ai_config(plain_key="sk-vp-secret-123456"):
    app_mod._save_ai_config({
        "base_url": "http://llm.example/v1",
        "api_key": plain_key,
        "model": "gpt-proxy",
        "api_protocol": "openai",
    })
    return plain_key


def _grant_proxy_slide(client, uid="usr_viewport_owner", slide="s.svs"):
    """同 test_ai_proxy._grant_proxy_slide：no-auth 下注入稳定 user_id。"""
    app_mod.share_store.set_slide_meta(slide, owner_user_id=uid)
    with client.session_transaction() as s:
        s["user_id"] = uid


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    isolate_app(monkeypatch, tmp_path)
    p = app_mod._ai_config_path()
    if p.is_file():
        p.unlink()
    yield


VALID_BBOX = {"x": 100, "y": 200, "w": 300, "h": 400}

# 非法形状 → (说明, 请求 body.viewport, 期望 400 文案片段)
INVALID_CASES = [
    ("非 dict（字符串）", "100,200,300,400", "需对象"),
    ("非 dict（列表）", [100, 200, 300, 400], "需对象"),
    ("缺键（无 h）", {"x": 1, "y": 2, "w": 3}, "h 需数值"),
    ("负尺寸", {"x": 1, "y": 2, "w": -3, "h": 4}, "w/h 必须为正"),
    ("零尺寸", {"x": 1, "y": 2, "w": 0, "h": 4}, "w/h 必须为正"),
    ("非数值", {"x": 1, "y": 2, "w": "3", "h": 4}, "w 需数值"),
    ("布尔数值", {"x": True, "y": 2, "w": 3, "h": 4}, "x 需数值"),
    ("负坐标", {"x": -1, "y": 2, "w": 3, "h": 4}, "x/y 不可为负"),
    ("超量级", {"x": 1, "y": 2, "w": 3, "h": 4e9}, "超出量级上限"),
]


def _register_run(fake, assertions=None):
    def handler(body, query, headers, kwargs):
        if assertions:
            assertions(body)
        return FakeResponse(200, sse_frames=[b"id: 1\nevent: agent_finished\ndata: {\"summary\":\"ok\"}\n\n"],
                            headers={"X-AI-Session-ID": "sess-vp-1"})
    fake.register("POST", "/run", handler)


def test_run_viewport_valid_passthrough_and_audit(monkeypatch):
    print("== run: 合法 viewport 原样透传 + 审计 detail 带 bbox ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    _grant_proxy_slide(client)

    def assert_forward(body):
        check("run 转发 payload 含原样 bbox", body.get("viewport") == VALID_BBOX,
              "got %r" % (body.get("viewport"),))
        check("viewport 与 task/session_id 同级（非 config 内嵌）",
              isinstance(body.get("viewport"), dict)
              and "viewport" not in (body.get("config") or {}))

    _register_run(fake, assert_forward)

    captured = {}
    orig_audit = app_mod._audit

    def fake_audit(action, **kw):
        captured.setdefault("details", []).append(kw.get("detail"))
        return orig_audit(action, **kw)

    monkeypatch.setattr(app_mod, "_audit", fake_audit)
    resp = client.post("/api/ai/run", json={"slide": "s.svs", "task": "分析当前视野",
                                            "viewport": dict(VALID_BBOX, extra="junk")})
    check("run 状态码 200", resp.status_code == 200, "got %d" % resp.status_code)
    vp_details = [d for d in captured.get("details", []) if isinstance(d, dict) and d.get("viewport")]
    check("审计 detail 记 bbox 四个数",
          vp_details and vp_details[0]["viewport"] == VALID_BBOX,
          "got %r" % (captured.get("details"),))


def test_run_viewport_invalid_shapes_400():
    print("== run: 非法 viewport 形状 → 400 invalid_argument 且不转发 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    _grant_proxy_slide(client)
    fake.register("POST", "/run", lambda b, q, h, k: FakeResponse(200, sse_frames=[]))

    for name, vp, msg_part in INVALID_CASES:
        fake.calls.clear()
        resp = client.post("/api/ai/run", json={"slide": "s.svs", "task": "分析", "viewport": vp})
        check("run %s → 400" % name, resp.status_code == 400, "got %d" % resp.status_code)
        data = json.loads(resp.data)
        check("run %s 稳定码 invalid_argument" % name, data.get("code") == "invalid_argument",
              "got %r" % (data.get("code"),))
        check("run %s 文案含 %r" % (name, msg_part), msg_part in str(data.get("error", "")),
              "got %r" % (data.get("error"),))
        check("run %s 未转发 sidecar（零副作用）" % name, len(fake.calls) == 0,
              "calls=%d" % len(fake.calls))


def test_run_without_viewport_unchanged():
    print("== run: 不带 viewport → 行为与现状一致（不透传该键）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    _grant_proxy_slide(client)

    def assert_no_vp(body):
        check("run 不带 viewport 时转发 body 无该键", "viewport" not in body)

    _register_run(fake, assert_no_vp)
    resp = client.post("/api/ai/run", json={"slide": "s.svs", "task": "看全片"})
    check("run 状态码 200（现状行为）", resp.status_code == 200, "got %d" % resp.status_code)
    # 显式 null 同样视为未携带（宽容缺省）。
    fake.calls.clear()
    resp2 = client.post("/api/ai/run", json={"slide": "s.svs", "viewport": None})
    check("run 显式 null 视同未携带 → 200", resp2.status_code == 200, "got %d" % resp2.status_code)
    check("run 显式 null 不透传", "viewport" not in (fake.calls[-1]["body"] if fake.calls else {}),
          "body=%r" % (fake.calls[-1]["body"] if fake.calls else None,))


def test_continue_viewport_passthrough_and_invalid():
    print("== continue: 合法透传 / 非法 400 ==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    _grant_proxy_slide(client)

    def cont_handler(body, query, headers, kwargs):
        check("continue 转发 payload 含原样 bbox", body.get("viewport") == VALID_BBOX,
              "got %r" % (body.get("viewport"),))
        return FakeResponse(200, sse_frames=[b"x\n\n"], headers={"X-AI-Session-ID": "sess-c"})

    fake.register("POST", "/continue", cont_handler)
    resp = client.post("/api/ai/continue", json={"slide": "s.svs", "viewport": dict(VALID_BBOX)})
    check("continue 状态码 200", resp.status_code == 200, "got %d" % resp.status_code)

    for name, vp, _msg in INVALID_CASES[:3]:
        fake.calls.clear()
        bad = client.post("/api/ai/continue", json={"slide": "s.svs", "viewport": vp})
        check("continue %s → 400" % name, bad.status_code == 400, "got %d" % bad.status_code)
        check("continue %s 未转发" % name, len(fake.calls) == 0, "calls=%d" % len(fake.calls))


def test_branch_viewport_passthrough_and_invalid():
    print("== branch: 合法透传 / 非法 400（branch 有工具，同 run/continue 口径）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()

    def branch_handler(body, query, headers, kwargs):
        check("branch 转发 payload 含原样 bbox", body.get("viewport") == VALID_BBOX,
              "got %r" % (body.get("viewport"),))
        return FakeResponse(200, sse_frames=[b"y\n\n"], headers={"X-AI-Session-ID": "sess-b"})

    fake.register("POST", "/branch", branch_handler)
    resp = client.post("/api/ai/branch",
                       json={"slide": "s.svs", "annotation_id": "br-1", "viewport": dict(VALID_BBOX)})
    check("branch 状态码 200", resp.status_code == 200, "got %d" % resp.status_code)

    for name, vp, _msg in INVALID_CASES[:3]:
        fake.calls.clear()
        bad = client.post("/api/ai/branch",
                          json={"slide": "s.svs", "annotation_id": "br-1", "viewport": vp})
        check("branch %s → 400" % name, bad.status_code == 400, "got %d" % bad.status_code)
        check("branch %s 未转发" % name, len(fake.calls) == 0, "calls=%d" % len(fake.calls))


def test_ask_never_forwards_viewport():
    print("== ask: fork 纯文本问答不透传 viewport（本切片有界范围）==")
    fake = install_fake_requests()
    client = make_client()
    setup_ai_config()
    _grant_proxy_slide(client)

    def ask_handler(body, query, headers, kwargs):
        check("ask 转发 payload 不含 viewport", "viewport" not in body,
              "got %r" % (body.get("viewport"),))
        return FakeResponse(200, sse_frames=[b"z\n\n"], headers={"X-AI-Session-ID": "sess-a"})

    fake.register("POST", "/ask", ask_handler)
    # 合法 viewport 也不透传。
    resp = client.post("/api/ai/ask",
                       json={"slide": "s.svs", "annotation_id": "ann-1",
                             "question": "这是什么", "viewport": dict(VALID_BBOX)})
    check("ask 带 viewport → 200 且不透传", resp.status_code == 200, "got %d" % resp.status_code)
    # 非法 viewport 同样静默忽略（不透传、不因 viewport 报错）。
    fake.calls.clear()
    resp2 = client.post("/api/ai/ask",
                        json={"slide": "s.svs", "annotation_id": "ann-1",
                              "question": "这是什么", "viewport": "junk"})
    check("ask 非法 viewport 静默忽略 → 200", resp2.status_code == 200, "got %d" % resp2.status_code)
    check("ask 非法 viewport 未透传", "viewport" not in fake.calls[-1]["body"],
          "body=%r" % (fake.calls[-1]["body"],))


if __name__ == "__main__":
    print("(直接运行模式请使用 pytest：.venv/bin/python -m pytest tests/test_ai_viewport.py)")
