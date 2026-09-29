# -*- coding: utf-8 -*-
"""源适配器（移植保真 + 重试/限速）与转换步骤测试。

真实 bdpan 适配器**只测纯逻辑**（路径约束/错码映射/脱敏/JSON fail-closed
与重试封装——monkeypatch 子进程执行，绝不起真 CLI、绝不触网）；端到端
一律走 fake 源。转换用共享原生核心真跑（合成 KFB/KFBF 夹具）。
"""

import pytest

from worker import errors
from worker.convert import (Converter, ConvertError, classify,
                            find_channel_json, sniff_artifact)
from worker.source.bdpan import (BdpanSource, _classify_cli_failure,
                                 _parse_entry, _redact, normalize_source_dir,
                                 validate_batch_relpath)
from worker.source.fake import FakeSource

pytestmark = pytest.mark.c5b


# --------------------------------------------------------------------------- #
# bdpan 移植纯逻辑
# --------------------------------------------------------------------------- #

def test_redact_strips_secrets_and_control_chars():
    out = _redact("url https://pan.baidu.com/s/SECRET code ABCD\x00tail",
                  "SECRET", "ABCD")
    assert "SECRET" not in out and "ABCD" not in out
    assert "\x00" not in out
    assert len(out) <= 300


def test_classify_cli_failure_priority():
    assert _classify_cli_failure("请先执行 bdpan login") == \
        "connector_unusable"
    assert _classify_cli_failure("提取码错误") == "share_password_error"
    assert _classify_cli_failure("分享不存在或已失效") == "share_invalid"
    assert _classify_cli_failure("disk on fire") == "connector_failed"


def test_validate_batch_relpath_rules():
    assert validate_batch_relpath("bch_1/a.kfb") == "bch_1/a.kfb"
    assert validate_batch_relpath("/apps/bdpan/bch_1/a.kfb") == \
        "bch_1/a.kfb"
    assert validate_batch_relpath("bch_1/a.kfb", batch_id="bch_1") == \
        "bch_1/a.kfb"
    for bad in ("https://pan.baidu.com/s/x", "/etc/passwd", "a/b/c",
                "bch_1/../a.kfb", "", "bch_1/", "bch_1/."):
        with pytest.raises(errors.SourceError) as ei:
            validate_batch_relpath(bad)
        assert ei.value.code == "cleanup_path_rejected"
    with pytest.raises(errors.SourceError):
        validate_batch_relpath("bch_2/a.kfb", batch_id="bch_1")


def test_parse_entry_fail_closed():
    ok = _parse_entry({"fs_id": 123, "isdir": False, "size": "45",
                       "server_filename": "a.kfb", "path": "/d/a.kfb"})
    assert ok["fs_id"] == "123" and ok["size"] == 45 and \
        ok["relative_path"] == "d/a.kfb"
    for bad in ({"fs_id": 1.5, "isdir": False},       # float fs_id 拒绝
                {"fs_id": "1"},                        # 缺 isdir
                {"fs_id": "1", "isdir": "no"},         # isdir 非布尔
                {"fs_id": "1", "isdir": True, "size": -2}):
        with pytest.raises(errors.SourceError) as ei:
            _parse_entry(bad)
        assert ei.value.code == "connector_output_invalid"


def test_normalize_source_dir():
    assert normalize_source_dir(None) is None
    assert normalize_source_dir("/") is None
    assert normalize_source_dir("a/b/") == "/a/b"
    assert normalize_source_dir("\\a\\b") == "/a/b"


# --------------------------------------------------------------------------- #
# 重试/限速（monkeypatch 子进程执行——绝不触网/不执行真 CLI）
# --------------------------------------------------------------------------- #

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, outcomes):
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        if item.returncode == 0 and len(argv) > 3 and argv[1] == "download":
            # 成功下载：落一个占位目标文件（适配器会核对存在性）
            from pathlib import Path
            dest = Path(argv[3])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / argv[2].rsplit("/", 1)[-1]).write_bytes(b"fakedata")
        return item

    monkeypatch.setattr("worker.source.bdpan.subprocess.run", fake_run)
    return calls


def test_bdpan_retry_on_transient(monkeypatch):
    import subprocess
    outcomes = [
        subprocess.TimeoutExpired(cmd="x", timeout=1),
        _FakeProc(1, "", "临时故障"),                 # → connector_failed
        _FakeProc(0, '{"status": "success"}', ""),
    ]
    calls = _patch_run(monkeypatch, outcomes)
    src = BdpanSource(bin_path="bdpan", max_attempts=3, backoff_base=0.001,
                      sleep=lambda s: None)
    src.download_to("bch_1/a.kfb", "/tmp/dest-c5b")
    assert len(calls) == 3
    assert src.counters()["retry"] == 2


def test_bdpan_no_retry_on_business_reject(monkeypatch):
    outcomes = [_FakeProc(1, "", "请先执行 bdpan login")]
    calls = _patch_run(monkeypatch, outcomes)
    src = BdpanSource(bin_path="bdpan", max_attempts=3, backoff_base=0.001,
                      sleep=lambda s: pytest.fail("不应重试业务拒绝"))
    with pytest.raises(errors.SourceError) as ei:
        src.download_to("bch_1/a.kfb", "/tmp/dest-c5b")
    assert ei.value.code == "connector_unusable"
    assert len(calls) == 1


def test_bdpan_throttle_min_interval(monkeypatch):
    outcomes = [
        _FakeProc(0, '{"list": [], "has_more": false}', ""),
        _FakeProc(0, '{"list": [], "has_more": false}', ""),
    ]
    _patch_run(monkeypatch, outcomes)
    waits = []
    clock = {"t": 0.0}

    def fake_sleep(s):
        waits.append(s)
        clock["t"] += s

    def fake_now():
        return clock["t"]

    src = BdpanSource(bin_path="bdpan", min_interval=0.5, sleep=fake_sleep,
                      now=fake_now)
    src.list_batch_copies("bch_1")
    src.list_batch_copies("bch_1")
    assert waits and waits[0] > 0  # 第二次操作被限速等待


def test_bdpan_argv_whitelist(monkeypatch):
    src = BdpanSource(bin_path="bdpan")
    with pytest.raises(errors.SourceError) as ei:
        src._run(["bdpan", "evil", "x"], 1)
    assert ei.value.code == "invalid_argv"
    with pytest.raises(errors.SourceError) as ei:
        src._run("not-a-list", 1)
    assert ei.value.code == "invalid_argv"


# --------------------------------------------------------------------------- #
# fake 源
# --------------------------------------------------------------------------- #

def test_fake_source_transfer_download_cleanup():
    src = FakeSource([
        {"path": "/d/a.kfb", "fs_id": "77", "size": 1000, "content":
         b"x" * 1000},
    ])
    page = src.list_share_page("https://pan.baidu.com/s/x", None, "/d",
                               None, 100)
    assert page["items"][0]["fs_id"] == "77"
    src.transfer_selected("bch", "https://pan.baidu.com/s/x", None, ["77"])
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        src.download_to("bch/a.kfb", d)
        import pathlib
        assert (pathlib.Path(d) / "a.kfb").stat().st_size == 1000
    src.cleanup_batch_copies("bch", ["bch/a.kfb"])
    assert src.copies["bch"] == {}
    assert src.counters()["download"] == 1


def test_fake_source_forbidden_in_production_env(monkeypatch):
    monkeypatch.setenv("PT_ENV", "production")
    src = FakeSource()
    caps = src.capabilities()
    assert caps["reason_code"] == "fake_adapter_forbidden_in_production"
    assert caps["import_available"] is False


# --------------------------------------------------------------------------- #
# 转换分类 / 伴随 / 嗅探
# --------------------------------------------------------------------------- #

def test_classify_matrix():
    assert classify("a.svs") == {"needs_convert": False,
                                 "format_ext": "svs", "filename": "a.svs"}
    assert classify("A.TIF")["format_ext"] == "tif"
    kfb = classify("sample-bf.kfb")
    assert kfb["needs_convert"] and kfb["filename"] == "sample-bf.tif"
    kfbf = classify("sample-fl.kfbf")
    assert kfbf["needs_convert"] and kfbf["filename"] == "sample-fl.ome.tif"
    for bad in ("a.mrxs", "a.zip", "a", ".kfb", "a.kfbz"):
        with pytest.raises(ConvertError):
            classify(bad)


def test_find_channel_json(tmp_path):
    src = tmp_path / "x.kfbf"
    src.write_bytes(b"0")
    assert find_channel_json(src) is None
    comp = tmp_path / "x_kfbf" / "Annotations" / "channel.json"
    comp.parent.mkdir(parents=True)
    comp.write_text("{}")
    assert find_channel_json(src) == comp


def test_sniff_artifact(tmp_path):
    tif = tmp_path / "a.tif"
    tif.write_bytes(b"II*\x00" + b"0" * 32)
    assert sniff_artifact(tif) == "tiff"
    jpg = tmp_path / "b.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0")
    assert sniff_artifact(jpg) == "jpeg"
    bad = tmp_path / "c.bin"
    bad.write_bytes(b"\x00" * 32)
    with pytest.raises(ConvertError):
        sniff_artifact(bad)


def test_converter_real_cli_kfb_and_kfbf(tmp_path, fixtures_dir, native_cli):
    """真跑共享原生核心：KFB→tif、KFBF→ome.tif（伴随 channel.json）。"""
    conv = Converter(str(native_cli), timeout=300)
    out_tif = tmp_path / "out-bf.tif"
    report = conv.convert(fixtures_dir["kfb"], out_tif)
    assert out_tif.is_file() and out_tif.stat().st_size > 0
    assert isinstance(report, dict)
    assert sniff_artifact(out_tif) == "tiff"
    # KFBF：伴随 channel.json 存在时传入
    out_ome = tmp_path / "out-fl.ome.tif"
    work = tmp_path / "w"
    (work / "sample-fl_kfbf" / "Annotations").mkdir(parents=True)
    (work / "sample-fl.kfbf").write_bytes(fixtures_dir["kfbf_bytes"])
    (work / "sample-fl_kfbf" / "Annotations" / "channel.json").write_bytes(
        fixtures_dir["channel_json"].read_bytes())
    report2 = conv.convert(work / "sample-fl.kfbf", out_ome,
                           find_channel_json(work / "sample-fl.kfbf"))
    assert out_ome.is_file() and out_ome.stat().st_size > 0
    assert isinstance(report2, dict)
    # 不存在的 CLI → 明确错误
    bad = Converter("/nonexistent/slide-transform")
    with pytest.raises(ConvertError) as ei:
        bad.convert(fixtures_dir["kfb"], tmp_path / "z.tif")
    assert ei.value.code == "converter_missing"
