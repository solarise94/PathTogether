# -*- coding: utf-8 -*-
"""journal（T12 持久日志）与 manifest 结构测试。"""

import json
import stat

import pytest

from worker import states
from worker.journal import Journal

pytestmark = pytest.mark.c5b


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_journal_roundtrip_and_permissions(tmp_path):
    j = Journal(tmp_path / "journal")
    rec = j.save({"import_id": "pim_1", "item_id": "itm_1",
                  "stage": states.DELIVERING, "write_token": "wt_secret"})
    assert rec["import_id"] == "pim_1"
    p = tmp_path / "journal" / "pim_1.json"
    assert p.is_file()
    assert _mode(p) == 0o600, "write_token 所在文件必须 0600"
    assert _mode(tmp_path / "journal") == 0o700
    loaded = Journal(tmp_path / "journal").load_all()
    assert loaded["pim_1"]["write_token"] == "wt_secret"
    # 合并式更新
    j.save({"import_id": "pim_1", "delivered_offset": 4096})
    loaded = Journal(tmp_path / "journal").load_all()
    assert loaded["pim_1"]["delivered_offset"] == 4096
    assert loaded["pim_1"]["write_token"] == "wt_secret"
    assert loaded["pim_1"]["stage"] == states.DELIVERING


def test_journal_by_item_active_only(tmp_path):
    j = Journal(tmp_path / "journal")
    j.save({"import_id": "pim_a", "item_id": "itm_a"})
    j.save({"import_id": "pim_b", "item_id": "itm_b",
            "terminal": {"kind": "failed", "code": "x"}})
    fresh = Journal(tmp_path / "journal")
    assert fresh.by_item("itm_a")["import_id"] == "pim_a"
    assert fresh.by_item("itm_b") is None


def test_journal_begin_pending_then_attach(tmp_path):
    j = Journal(tmp_path / "journal")
    pending = j.begin_pending(
        item_id="itm_1", batch_id="bch_1", grant_id="pig_1",
        project_id="prj_1", idempotency_key="key-1", filename="a.tif",
        format_ext="tif", declared_size=10, source_name="a.svs",
        source_size=10, needs_convert=False)
    assert pending["import_id"] == "pending-itm_1"
    assert j.by_item("itm_1")["idempotency_key"] == "key-1"
    rec = j.attach_import("pending-itm_1", import_id="pim_real",
                          write_token="wt_x", chunk_max_bytes=65536,
                          slide_id="sld_1")
    assert rec["import_id"] == "pim_real"
    assert rec["write_token"] == "wt_x"
    # pending 文件已被真实 import 记录取代
    assert not (tmp_path / "journal" / "pending-itm_1.json").is_file()
    assert (tmp_path / "journal" / "pim_real.json").is_file()
    assert Journal(tmp_path / "journal").by_item("itm_1")["import_id"] == \
        "pim_real"


def test_journal_strip_secret_keeps_rest(tmp_path):
    j = Journal(tmp_path / "journal")
    j.save({"import_id": "pim_1", "item_id": "itm_1",
            "write_token": "wt_secret", "stage": states.DONE,
            "receipt": {"state": "published", "slide_id": "sld_1"}})
    j.strip_secret("pim_1")
    data = json.loads((tmp_path / "journal" / "pim_1.json").read_text())
    assert "write_token" not in data
    assert data["receipt"]["slide_id"] == "sld_1"


def test_journal_corrupt_file_skipped(tmp_path):
    d = tmp_path / "journal"
    d.mkdir(parents=True)
    (d / "bad.json").write_text("{not json")
    (d / "good.json").write_text(json.dumps(
        {"import_id": "pim_g"}))
    loaded = Journal(d).load_all()
    assert set(loaded) == {"pim_g"}


def test_journal_atomic_no_tmp_left(tmp_path):
    j = Journal(tmp_path / "journal")
    j.save({"import_id": "pim_1", "item_id": "itm_1"})
    leftovers = [p for p in (tmp_path / "journal").iterdir()
                 if p.name.startswith(".jnl-")]
    assert leftovers == []


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

def test_manifest_shape():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    m = json.loads((root / "manifest.json").read_text("utf-8"))
    import re
    semver = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    for field in ("manifestSchemaVersion", "pluginVersion",
                  "pluginContractVersion", "bridgeProtocolVersion"):
        assert semver.match(m[field]), field
    assert m["id"] == "dev.pathtogether.baidu-import"
    assert m["permissions"] == ["slide:import"], \
        "本插件只申请 slide:import（§2.1）"
    assert m["ui"]["entry"].startswith("/plugins/pathtogether-baidu-import/")
    assert m["ui"]["slots"]
    assert m["service"]["baseUrl"].startswith("http://127.0.0.1:")
    assert m["service"]["health"] == "/healthz"
    # 不声明 provides：C5 不向 agent 注入导入写能力（§2.6）
    assert "provides" not in m
    assert "adminPermissions" not in m


def test_states_transitions():
    assert states.can_advance(states.QUEUED, states.DOWNLOADING)
    assert states.can_advance(states.DOWNLOADING, states.CLEANUP_PENDING)
    assert not states.can_advance(states.DONE, states.DOWNLOADING)
    assert states.can_advance(states.DELIVERING, states.DELIVERING)
    assert states.TERMINAL == {"done", "failed", "cancelled"}
