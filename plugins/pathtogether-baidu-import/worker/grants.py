# -*- coding: utf-8 -*-
"""用户导入委托 grant 登记（C5-B；合同 §2.2）。

用户在平台用户面 ``POST /api/plugin/import-grants``（Cookie+CSRF）创建
grant（``pig_`` 前缀，绑定 installation+user+project，默认 TTL 24h），
一次性展示后**由用户粘进插件**（插件 UI 引导；正式入口是本文件 + UI）。
grant_id 属凭证类：文件 0600，绝不进日志。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_GRANT_ID_RE = re.compile(r"^pig_[A-Za-z0-9_-]{4,128}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class GrantRegistry:
    """project_id → grant_id（安装级；同用户可对同项目持多 grant，登记
    存最后写入的一个；grant 过期/撤销由平台在 begin 时拒绝）。"""

    def __init__(self, path, *, seed=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._data = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    self._data = {
                        str(k): str(v) for k, v in loaded.items()}
            except (OSError, ValueError):
                self._data = {}
        # seed：env/测试注入（"proj:pig_xxx,proj2:pig_yyy"）
        for pair in (seed or "").split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            project, _, grant = pair.partition(":")
            self.put(project.strip(), grant.strip())

    def put(self, project_id, grant_id):
        """登记/替换某项目的 grant（校验形态；非法即拒）。"""
        if not _PROJECT_ID_RE.match(project_id or ""):
            raise ValueError("project_id 形态非法")
        if not _GRANT_ID_RE.match(grant_id or ""):
            raise ValueError("grant_id 形态非法（pig_ 前缀）")
        self._data[project_id] = grant_id
        self._flush()

    def grant_for(self, project_id):
        return self._data.get(str(project_id)) or None

    def remove(self, project_id):
        self._data.pop(str(project_id), None)
        self._flush()

    def projects(self):
        return sorted(self._data)

    def _flush(self):
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".grants-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False,
                          sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, str(self.path))
            tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
