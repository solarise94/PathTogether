# -*- coding: utf-8 -*-
"""排空核验工具测试（U4→U5→R16 修订；检查点 A 与冻结清单已取消）。

检查点 B 后旧上传端点已删除：本文件覆盖部署门禁——
- scripts/upload_drain.py：audit（pending 非异常、暂存异常 exit 3）/
  report（未收口 no-go exit 3；收口后 go exit 0）——排空证明的可复跑
  副本演练等价物（历史 HTTP 门禁用例随端点删除退役，场景由
  test_ingestion_api/test_cos_ingestion_kinds 在统一链路覆盖）。
"""

import importlib.util
import json
import shutil
from pathlib import Path

import upload_task_store

_REPO = Path(__file__).resolve().parent.parent


def _mk_task(name, size=64):
    return upload_task_store.create_task(
        "", name, name, size, upload_task_store.UPLOAD_CHUNK_SIZE)


def _drain_tool():
    spec = importlib.util.spec_from_file_location(
        "upload_drain_tool", _REPO / "scripts" / "upload_drain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_no_go_then_go(tmp_path):
    tool = _drain_tool()
    t = _mk_task("report-a.svs")

    # audit：在途任务是 pending（非异常）→ exit 0
    assert tool.main(["audit", "--upload-dir", str(tmp_path)]) == 0
    # report：未收口 → no-go
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 3
    out_json = tmp_path / "report.json"
    assert tool.main(["report", "--upload-dir", str(tmp_path),
                      "--json", str(out_json)]) == 3
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["pending"]  # old_task_pending 在列

    # 取消收口（store 级；清理确认后释放）→ go
    upload_task_store.cancel_task(t["upload_id"])
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 0


def test_report_anomaly_staging(tmp_path):
    tool = _drain_tool()
    orphan = tmp_path / ".staging" / "upt_orphan0001"
    orphan.mkdir(parents=True)
    (orphan / "data.svs").write_bytes(b"x")
    assert tool.main(["audit", "--upload-dir", str(tmp_path)]) == 3
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 3
    shutil.rmtree(tmp_path / ".staging", ignore_errors=True)
    assert tool.main(["report", "--upload-dir", str(tmp_path)]) == 0
