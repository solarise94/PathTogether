# -*- coding: utf-8 -*-
"""项目创建 HTTP 装配助手（W3）。

app.py 的 ``/api/project/create`` 由协调者接线（本模块不改 app.py）。接线
方式（与既有路由语义对齐）::

    ident = current_identity()
    body = request.get_json(silent=True) or {}
    key = request.headers.get("Idempotency-Key")
    clean, err = _validate_slide_names(
        body.get("slides") if isinstance(body.get("slides"), list) else None)
    if err:
        return jsonify(error=err), 400
    payload, status = handle_create(ident, body, key, slides_override=clean)
    return jsonify(payload), status

分工：所有权/可写性校验（``_validate_slide_names``）留在 HTTP 层——本助手
只做形状校验（400）、guest 拒绝（403）与幂等写入（409）。
"""

import project_idempotency_store as _store


def handle_create(ident, body, idempotency_key, slides_override=None):
    """执行创建，返回 ``(payload_dict, status_code)``。

    - ``ident``：``current_identity()`` dict（至少含 user_id / role）；
    - ``body``：JSON dict（{name, note?, slides?}）；
    - ``idempotency_key``：请求头 ``Idempotency-Key``（可 None=旧行为）；
    - ``slides_override``：HTTP 层经 ``_validate_slide_names`` 清洗后的
      slide 名列表；给出时**替换** body.slides（body 只作形状来源）。
    """
    try:
        name, note, _slides = _store.validate_create_payload(body)
        slides = slides_override if slides_override is not None else _slides
    except _store.PayloadInvalid as exc:
        return {"error": str(exc)}, 400
    try:
        proj = _store.create_project_idempotent(
            name=name, note=note, slides=slides,
            owner_user_id=ident["user_id"], requester_role=ident["role"],
            idempotency_key=idempotency_key)
    except _store.IdempotencyConflict as exc:
        return {"error": str(exc), "code": exc.code}, 409
    except PermissionError:
        return {"error": "无权创建项目"}, 403
    # duplicate=True 是内部标记；HTTP 响应与首次创建同形状，去掉即可
    proj.pop("duplicate", None)
    return proj, 200
