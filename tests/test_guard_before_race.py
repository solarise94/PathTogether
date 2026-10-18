# -*- coding: utf-8 -*-
"""旧 index 路由 annotation_id 防护「二次定位」竞态的回归（2026-10-18 P2）。

缺陷（21bd1720）：app.py 旧 index 路由已改为只 get_roi 一次、对该行鉴权、
写入绑定该行 annotation_id，但防护 ``_guard_index_annotation_id`` 内部仍按
index **二次** get_roi。路由解析行与防护比对之间列表一旦漂移（并发删了
前面的行），防护比对的是**另一行**：带 annotation_id=B1 的
DELETE /api/annotation/admin/1 返回 200，实际删掉的是已解析行 A2，B1 仍活
（评审复现 run-review1018-Zw4iud/test_guard_before_race.py）。

修复契约（本文件锁定）：防护签名改为 ``_guard_index_annotation_id(roi,
expected_aid)``——只比对调用方已解析、已鉴权的行，自身绝不再查存储。
请求 id 与该行不一致 → 409 annotation_moved（annotation_id 回显该行 id）；
一致或未携带 → 放行，写入绑定该行（响应码/响应体与修复前一致）。
share_server._guard_roi_index_annotation_id(r, expected_aid) 本即此口径
（镜像来源），无需改动。

用例：
  - ``test_review_id_guard_must_validate_the_authorized_row``：评审复现
    原样迁入（断言不动，仅钩子适配新防护签名）；
  - 参数化矩阵：app.py 全部 7 条调用防护的旧 index 路由 × 两种注入。

运行：cd 项目根 && python3 -m pytest tests/test_guard_before_race.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import _bootstrap  # noqa: E402,F401  # session 目录 + openslide stub（conftest 先行）
DATA_DIR = _bootstrap.SHARE_DATA_DIR
UPLOAD_DIR = _bootstrap.UPLOAD_DIR
import share_store  # noqa: E402
import user_store  # noqa: E402
import app as app_mod  # noqa: E402
import share_server as share_srv  # noqa: E402
from _pt_helpers import csrf_client, isolate_app, register_slide_row  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    _, up_dir = isolate_app(monkeypatch, DATA_DIR, UPLOAD_DIR,
                            clear_stores=True)
    for child in up_dir.iterdir():
        if child.is_file():
            child.unlink()
    # 分享 ROI 写入的服务端尺寸/MPP 复核需要可信元数据（同
    # test_annotation_index_write_binding）
    monkeypatch.setattr(
        share_srv, "_slide_dims_and_mpp",
        lambda safe, path=None: (100000, 100000, 60.0, 60.0))
    yield


# --------------------------------------------------------------------------- #
# 世界构造（同 test_annotation_index_write_binding.py）：owner + 用户 A/B；
# 切片 X（A 有，public）与 Y（B 有）。工作台标注全落在共享 token=admin 下。
# --------------------------------------------------------------------------- #
def _client():
    app_mod.app.config["TESTING"] = True
    app_mod.AUTH_ENABLED = True
    return csrf_client(app_mod.app.test_client())


def _login(client, login_id, password):
    return client.post("/login", data={"username": login_id, "password": password})


def _client_as(login_id, password):
    c = _client()
    assert _login(c, login_id, password).status_code in (200, 302)
    return c


def _touch(name):
    p = Path(UPLOAD_DIR) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"svs-stub")
    register_slide_row(name, upload_dir=UPLOAD_DIR)  # P6：建可读仓（id_bundle 行）
    return name


def _setup_world():
    owner = user_store.create_user("owner@x.com", "ownerpass123456", role="owner")
    user_a = user_store.create_user("a@x.com", "userApass123456", role="user")
    user_b = user_store.create_user("b@x.com", "userBpass123456", role="user")
    x = _touch("x.svs")
    y = _touch("y.svs")
    share_store.set_slide_meta(x, owner_user_id=user_a["user_id"], public=True)
    share_store.set_slide_meta(y, owner_user_id=user_b["user_id"])
    return owner, user_a, user_b, x, y


def _anno(client, slide, label="L"):
    """工作台人工标注（token=admin，source=human）；index 取 live 口径。"""
    r = client.post("/api/annotation", json={
        "slide": slide, "type": "rect", "label": label,
        "x": 10, "y": 10, "side_px": 100, "size_mm": 6.0,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    return {"annotation_id": body["annotation_id"],
            "index": _live_indexes()[body["annotation_id"]]}


def _anno_ai(slide, owner_user_id, label="AI"):
    """AI 待审标注（store 直写，source=ai）；index 取 live 口径。"""
    roi = share_store.add_roi(
        share_store.ADMIN_TOKEN, slide, label, type="rect",
        x=20, y=20, side_px=100, size_mm=6.0,
        source="ai", owner_user_id=owner_user_id)
    assert roi.get("source") == "ai"
    return {"annotation_id": roi["annotation_id"],
            "index": _live_indexes()[roi["annotation_id"]]}


def _live_indexes():
    """admin token 当前 live 行的 {annotation_id: index}（get_roi 口径）。"""
    return {r["annotation_id"]: r["index"]
            for r in share_store.list_rois(share_store.ADMIN_TOKEN)}


def _share_client():
    share_srv.app.config["TESTING"] = True
    return share_srv.app.test_client()


def _visitor_anno(v, tok, label):
    """访客建一条标记；POST 响应只带 index → 按 label 从列表回读 id/index。"""
    r = v.post("/s/%s/api/roi" % tok,
               json={"slide": "x.svs", "type": "rect", "label": label,
                     "x": 0, "y": 0, "side_px": 100, "size_mm": 6.0})
    assert r.status_code == 200, r.get_data(as_text=True)
    listed = [i for i in v.get("/s/%s/api/rois" % tok).get_json()
              if i.get("source") == "me" and i.get("label") == label]
    assert len(listed) == 1
    it = listed[0]
    return {"annotation_id": it["annotation_id"], "index": it["index"]}


def _install_shift_before_guard(monkeypatch, victim_aid):
    """包住 app 的 index 防护：进入比对**之前**删掉 victim 行。

    稳定复现 P2 缺陷窗口——路由已解析/鉴权某行，并发请求此刻删掉前面的行
    → live 序前移；旧防护按 index 二次定位会比对到另一行。
    """
    original = app_mod._guard_index_annotation_id

    def shift_before_guard(roi, aid):
        assert share_store.delete_roi_by_annotation_id(victim_aid)
        return original(roi, aid)

    monkeypatch.setattr(app_mod, "_guard_index_annotation_id",
                        shift_before_guard)


# =========================================================================== #
# 评审复现（断言原样；钩子适配新防护签名）
# =========================================================================== #
def test_review_id_guard_must_validate_the_authorized_row(monkeypatch):
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    co = _client_as("owner@x.com", "ownerpass123456")
    a1 = _anno(ca, x, "A1")
    a2 = _anno(ca, x, "A2")
    b1 = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
    original = app_mod._guard_index_annotation_id

    def shift_before_guard(roi, aid):
        # The route already resolved/authorized A2; a concurrent request now
        # removes A1, so the old guard's second lookup at index=1 resolves B1.
        assert share_store.delete_roi_by_annotation_id(a1["annotation_id"])
        return original(roi, aid)

    monkeypatch.setattr(app_mod, "_guard_index_annotation_id",
                        shift_before_guard)
    response = co.delete("/api/annotation/admin/1",
                         json={"annotation_id": b1["annotation_id"]})
    intended = share_store.get_roi_by_annotation_id(b1["annotation_id"])
    other = share_store.get_roi_by_annotation_id(a2["annotation_id"])
    assert not other.get("deleted"), (response.status_code,
                                      response.get_json(),
                                      "Requested B1 but deleted A2", intended)


# =========================================================================== #
# 参数化矩阵：全部 7 条旧 index 路由，防护比对必须绑定已解析行
# =========================================================================== #
_INDEX_ROUTES = ("delete_admin", "delete_token", "patch", "comments_get",
                 "comments_post", "review", "history")


@pytest.mark.parametrize("op", _INDEX_ROUTES)
@pytest.mark.parametrize("scenario", ("blocked_mismatch", "applied_match"))
def test_index_guard_binds_resolved_row(monkeypatch, scenario, op):
    """路由解析/鉴权之后、防护比对之前并发删掉前面的行（w1）：

      - blocked_mismatch：请求指认另一行（oth）→ 409 annotation_moved，
        annotation_id 回显**已解析行** id；已解析行与指认行都完好（旧实现
        按前移 index 二次定位 → 比对指认行放行 → 写到已解析行上）；
      - applied_match：请求指认已解析行 → 不得误报 409，操作落在已解析
        行上，另一行完好（旧实现会比对到前移后的另一行 → 误报 409）。
    """
    owner, user_a, user_b, x, y = _setup_world()
    ca = _client_as("a@x.com", "userApass123456")
    co = _client_as("owner@x.com", "ownerpass123456")
    if op == "review":
        w1 = _anno_ai(x, user_a["user_id"], "AI-1")
        tgt = _anno_ai(x, user_a["user_id"], "AI-2")
        oth = _anno_ai(x, user_a["user_id"], "AI-3")
        client, tok = ca, share_store.ADMIN_TOKEN
    elif op == "delete_token":
        stok = share_store.create_share(
            [x], 24, permissions=["view", "annotate"])["token"]
        v = _share_client()
        w1 = _visitor_anno(v, stok, "V1")
        tgt = _visitor_anno(v, stok, "V2")
        oth = _visitor_anno(v, stok, "V3")
        client, tok = co, stok  # 访客行归 owner 写权（_check_annotation_owner_row）
    else:
        w1 = _anno(ca, x, "A1")
        tgt = _anno(ca, x, "A2")
        oth = _anno(_client_as("b@x.com", "userBpass123456"), y, "B1")
        client, tok = ca, share_store.ADMIN_TOKEN
    assert (w1["index"], tgt["index"], oth["index"]) == (0, 1, 2)
    _install_shift_before_guard(monkeypatch, w1["annotation_id"])

    tgt_id, oth_id, index = tgt["annotation_id"], oth["annotation_id"], \
        tgt["index"]
    seed_cmt = None
    if op == "comments_get":
        # 预置已解析行的评论：返回它才证明读绑定已解析行（oth 名下无评论）
        seed_cmt = share_store.add_comment(tgt_id, x, tok, "seed",
                                           author_user_id=user_a["user_id"])
    elif op == "history":
        # 预置已解析行的历史：非空历史才证明读绑定已解析行（oth 无历史）
        assert share_store.update_roi_by_annotation_id(tgt_id, note="v1")

    aid = tgt_id if scenario == "applied_match" else oth_id
    if op == "delete_admin":
        r = client.delete("/api/annotation/admin/%d" % index,
                          json={"annotation_id": aid})
    elif op == "delete_token":
        r = client.delete("/api/annotation/%s/%d" % (tok, index),
                          json={"annotation_id": aid})
    elif op == "patch":
        r = client.patch("/api/annotation/%s/%d" % (tok, index),
                         json={"note": "v2", "annotation_id": aid})
    elif op == "comments_get":
        r = client.get("/api/annotation/%s/%d/comments?annotation_id=%s"
                       % (tok, index, aid))
    elif op == "comments_post":
        r = client.post("/api/annotation/%s/%d/comments" % (tok, index),
                        json={"body": "hey", "annotation_id": aid})
    elif op == "review":
        r = client.post("/api/annotation/%s/%d/review" % (tok, index),
                        json={"action": "accept", "annotation_id": aid})
    else:  # history
        r = client.get("/api/annotation/%s/%d/history?annotation_id=%s"
                       % (tok, index, aid))

    def tgt_row():
        """已解析行落库现状。"""
        return share_store.get_roi_by_annotation_id(tgt_id) or {}

    def oth_row():
        """指认行落库现状。"""
        return share_store.get_roi_by_annotation_id(oth_id) or {}

    if scenario == "blocked_mismatch":
        assert r.status_code == 409, (op, r.status_code,
                                      r.get_data(as_text=True))
        body = r.get_json()
        assert body["error"] == "annotation_moved"
        assert body["annotation_id"] == tgt_id, \
            "409 未回显已解析行的 id（防护没有比对已解析行）"
        assert tgt_row().get("deleted") is False, \
            "指认另一行却写到了已解析行上（防护未绑定已解析行）"
        assert oth_row().get("deleted") is False
        if op == "patch":
            assert tgt_row().get("note") == "" and oth_row().get("note") == ""
        elif op == "review":
            assert tgt_row().get("review_status") == "pending"
            assert oth_row().get("review_status") == "pending"
        elif op == "comments_post":
            assert share_store.list_comments(annotation_id=tgt_id) == []
            assert share_store.list_comments(annotation_id=oth_id) == []
    else:  # applied_match：指认即已解析行 → 不得误报 409，操作落已解析行
        assert r.status_code == 200, (op, r.status_code,
                                      r.get_data(as_text=True))
        assert oth_row().get("deleted") is False, "另一行被误写"
        if op in ("delete_admin", "delete_token"):
            assert tgt_row().get("deleted") is True, "删除未落在已解析行上"
        elif op == "patch":
            assert tgt_row().get("note") == "v2", "备注未落在已解析行上"
            assert oth_row().get("note") == ""
        elif op == "review":
            assert tgt_row().get("review_status") == "accepted", \
                "审核未落在已解析行上"
            assert oth_row().get("review_status") == "pending"
        elif op == "comments_post":
            assert [c["comment_id"] for c in
                    share_store.list_comments(annotation_id=tgt_id)] != [], \
                "评论未挂在已解析行下"
            assert share_store.list_comments(annotation_id=oth_id) == []
        elif op == "comments_get":
            cids = [c["comment_id"] for c in r.get_json()["comments"]]
            assert seed_cmt["comment_id"] in cids, "返回的不是已解析行的评论"
        else:  # history
            assert r.get_json()["history"], "返回的不是已解析行的历史"
