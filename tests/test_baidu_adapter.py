# -*- coding: utf-8 -*-
"""W5 B01/B02：百度分享解析器与适配器合同测试。

B01：分享文本/提取码/冲突/非允许域名；参数不经 shell（argv 恒列表）。
B02：固定 CLI 输出 fixture（分页/目录/fs_id/大整数）；超时/非零退出/
未知 JSON 明确失败（绝不把解析异常当空分享）。

不访问网络、不调用真实连接器（capabilities 探测用 /bin/true 等本地
无副作用二进制）。
"""
import json
import subprocess
from pathlib import Path

import pytest

import baidu_adapter as ba
from baidu_adapter import (AdapterError, FakeBaiduAdapter,
                           ProductionBaiduAdapter, get_adapter)
import baidu_share_parser as bp

FIXTURES = Path(__file__).parent / "fixtures" / "baidu_cli"

SHARE = "https://pan.baidu.com/s/1TestShareId99"


def _load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# B01：解析器
# --------------------------------------------------------------------------- #

def test_b01_parse_plain_url():
    out = bp.parse_share_text("https://pan.baidu.com/s/1TestShareId99")
    assert out == {"share_url": SHARE, "share_id": "1TestShareId99",
                   "extraction_code": None}


def test_b01_parse_pwd_from_query():
    out = bp.parse_share_text("https://pan.baidu.com/s/1AbCdE?pwd=Zx9y")
    assert out["share_url"] == "https://pan.baidu.com/s/1AbCdE"
    assert out["extraction_code"] == "zx9y"


def test_b01_parse_extraction_code_from_text():
    for text in ("提取码: ab12", "提取码：ab12",
                 "链接 https://pan.baidu.com/s/1TestShareId99 提取码: ab12",
                 "pwd=ab12"):
        out = bp.parse_share_text(
            "https://pan.baidu.com/s/1TestShareId99 " + text
            if "pan.baidu" not in text else text)
        assert out["extraction_code"] == "ab12", text


def test_b01_extraction_code_conflict():
    # query pwd 与正文提取码冲突 / 与显式入参冲突 → 400（extraction_code_conflict）
    with pytest.raises(bp.ShareParseError) as ei:
        bp.parse_share_text(
            "https://pan.baidu.com/s/1TestShareId99?pwd=aa11 提取码: bb22")
    assert ei.value.code == "extraction_code_conflict"
    with pytest.raises(bp.ShareParseError) as ei2:
        bp.parse_share_text("https://pan.baidu.com/s/1TestShareId99?pwd=aa11",
                            extraction_code="bb22")
    assert ei2.value.code == "extraction_code_conflict"
    # 冲突消息不携带任何提取码明文（脱敏）
    assert "aa11" not in str(ei.value) and "bb22" not in str(ei.value)


def test_b01_non_baidu_domain_rejected():
    for text in ("https://evil.com/s/1TestShareId99",
                 "https://pan.baidu.com.evil.com/s/1TestShareId99?pwd=ab12",
                 "看看 http://example.com/s/xyz"):
        with pytest.raises(bp.ShareParseError) as ei:
            bp.parse_share_text(text)
        assert ei.value.code == "unsupported_share", text


def test_b01_invalid_share_text():
    with pytest.raises(bp.ShareParseError) as ei:
        bp.parse_share_text("没有链接的文本")
    assert ei.value.code == "invalid_share_text"
    # 非法显式提取码 fail-closed
    with pytest.raises(bp.ShareParseError) as ei2:
        bp.parse_share_text(SHARE, extraction_code="太长了的提取码啊")
    assert ei2.value.code == "extraction_code_invalid"


def test_b01_explicit_code_matches_query_ok():
    out = bp.parse_share_text(SHARE + "?pwd=ab12", extraction_code="AB12")
    assert out["extraction_code"] == "ab12"  # 大小写不敏感一致 → 不算冲突


# --------------------------------------------------------------------------- #
# B01：生产适配器 argv 安全（参数数组、无 shell、白名单）
# --------------------------------------------------------------------------- #

class _CapRun:
    """捕获 subprocess.run 调用（argv/kwargs），返回可空 stdout。"""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("BAIDU_CONNECTOR_BIN", "/bin/true")
    return ProductionBaiduAdapter(bin_path="/bin/true")


def test_b01_argv_is_plain_list_no_shell(prod, monkeypatch):
    cap = _CapRun(stdout=_load("list_page_root.json"))
    monkeypatch.setattr(ba.subprocess, "run", cap)
    out = prod.list_share_page(SHARE, "ab12", None, None, 100)
    assert len(cap.calls) == 1
    call = cap.calls[0]
    # argv 是纯字符串列表，subprocess 未启用 shell
    assert isinstance(call["argv"], list)
    assert all(isinstance(a, str) for a in call["argv"])
    assert not call["kwargs"].get("shell")
    # 白名单子命令 + 位置参数在前、flag 带 URL/code
    argv = call["argv"]
    assert argv[:3] == ["/bin/true", "transfer", "list"]
    assert argv[3] == SHARE
    assert "--pwd" in argv and argv[argv.index("--pwd") + 1] == "ab12"
    assert out["next_cursor"] == "2" and out["has_more"] is True


def test_b01_malicious_share_never_reaches_subprocess(prod, monkeypatch):
    cap = _CapRun()
    monkeypatch.setattr(ba.subprocess, "run", cap)
    for bad in ("https://evil.com/s/x; rm -rf /",
                SHARE + "\"; touch /tmp/pwned",
                "https://pan.baidu.com/s/../etc/passwd"):
        with pytest.raises(AdapterError) as ei:
            prod.list_share_page(bad, None, None, None, 100)
        assert ei.value.code == "invalid_share"
    assert cap.calls == []  # 恶意输入在 argv 构造前被拒绝


def test_b01_transfer_select_argv(prod, monkeypatch):
    cap = _CapRun(stdout=_load("transfer_select.json"))
    monkeypatch.setattr(ba.subprocess, "run", cap)
    prod.transfer_selected("bib_demo", SHARE, None, ["910700000000000001"])
    argv = cap.calls[0]["argv"]
    assert argv[:3] == ["/bin/true", "transfer", "select"]
    assert argv[argv.index("--fsid") + 1] == "910700000000000001"
    assert argv[argv.index("--dir") + 1] == "bib_demo"
    with pytest.raises(AdapterError):
        prod.transfer_selected("bib_demo", SHARE, None, ["not-numeric;ls"])


# --------------------------------------------------------------------------- #
# B02：固定 CLI 输出 fixture → 规范化解析
# --------------------------------------------------------------------------- #

def test_b02_list_page_root_fixture(prod, monkeypatch):
    monkeypatch.setattr(
        prod, "_run",
        lambda *a, **k: _load("list_page_root.json"))
    out = prod.list_share_page(SHARE, None, None, None, 100)
    items = {i["name"]: i for i in out["items"]}
    assert items["批次日结"]["is_dir"] is True
    assert items["样本A.svs"]["fs_id"] == "910700000000000001"
    assert items["样本A.svs"]["size"] == 12345678901234  # 大整数无损
    assert items["样本A.svs"]["relative_path"] == "样本A.svs"
    assert out["has_more"] is True and out["next_cursor"] == "2"


def test_b02_list_page_live_items_shape(prod, monkeypatch):
    """bdpan 3.8.7 实测：transfer list --json 为 items/is_dir/name，不是 list/isdir。"""
    monkeypatch.setattr(
        prod, "_run",
        lambda *a, **k: _load("list_page_items.json"))
    out = prod.list_share_page(SHARE, None, None, None, 50)
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["fs_id"] == "379673321351613"
    assert it["is_dir"] is False
    assert it["name"] == "pt-bdpan-smoke.txt"
    assert it["size"] == 35
    assert out["has_more"] is False and out["next_cursor"] is None


def test_b02_list_page_nested_fixture(prod, monkeypatch):
    monkeypatch.setattr(
        prod, "_run", lambda *a, **k: _load("list_page_nested.json"))
    out = prod.list_share_page(SHARE, None, "/批次日结", "2", 100)
    items = {i["relative_path"]: i for i in out["items"]}
    assert items["批次日结/scan.ome.tif"]["size"] == 806456145
    assert items["批次日结/panel1_kfbf"]["is_dir"] is True
    assert out["has_more"] is False and out["next_cursor"] is None


def test_b02_large_integer_fs_id_and_size(prod, monkeypatch):
    # fs_id 超过 float53 精度（2^53+1）与 BIGINT 上限形态：字符串原样、
    # int 任意精度 → 转 str 后不丢精度
    monkeypatch.setattr(
        prod, "_run",
        lambda *a, **k: _load("list_page_large_ids.json"))
    out = prod.list_share_page(SHARE, None, None, None, 100)
    by_path = {i["relative_path"]: i for i in out["items"]}
    assert by_path["超过float53精度的fs_id.svs"]["fs_id"] == "9007199254740993"
    assert by_path["超大字符串fs_id.tif"]["fs_id"] == "9223372036854775807"
    assert by_path["超大字符串fs_id.tif"]["size"] == 9223372036854775807


def test_b02_ls_batch_fixture_numeric_fs_id(prod, monkeypatch):
    # ls --json 官方形态：裸数组 + 数字 fs_id → 字符串化，绝不 float
    monkeypatch.setattr(prod, "_run", lambda *a, **k: _load("ls_batch.json"))
    copies = prod.list_batch_copies("bib_demo")
    by_name = {c["name"]: c for c in copies}
    assert by_name["大切片.svs"]["fs_id"] == "9007199254740993"
    assert by_name["panel1.kfbf"]["size"] == 481661346
    assert all(c["relative_path"].startswith("bib_demo/") for c in copies)


def test_b02_transfer_select_fixture(prod, monkeypatch):
    monkeypatch.setattr(
        prod, "_run", lambda *a, **k: _load("transfer_select.json"))
    out = prod.transfer_selected("bib_demo", SHARE, None, ["524080722157776"])
    assert out["task_id"].startswith("btt_")
    assert prod.poll_transfer(out["task_id"]) == {"state": "succeeded"}
    # 进程重启（登记丢失）→ unknown，调用方必须先对账，不得盲目重转存
    fresh = ProductionBaiduAdapter(bin_path="/bin/true")
    assert fresh.poll_transfer(out["task_id"]) == {"state": "unknown"}


def test_b02_timeout_is_explicit_failure(prod, monkeypatch):
    # 超时穿透真实 _run 的捕获路径（subprocess.run → TimeoutExpired →
    # AdapterError(connector_timeout)）
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(["bdpan"], 0.01)
    monkeypatch.setattr(ba.subprocess, "run", boom)
    with pytest.raises(AdapterError) as ei:
        prod.list_share_page(SHARE, None, None, None, 100)
    assert ei.value.code == "connector_timeout"


def test_b02_nonzero_exit_failure_redacted(prod, monkeypatch):
    def boom(argv, timeout, secrets=()):
        raise AdapterError(
            "connector_failed",
            ba._redact("exit=1 分享 %s 码 %s 失败" % (secrets[0], secrets[1]),
                       *secrets))
    monkeypatch.setattr(prod, "_run", boom)
    with pytest.raises(AdapterError) as ei:
        prod.list_share_page(SHARE, "ab12", None, None, 100)
    assert ei.value.code == "connector_failed"
    assert SHARE not in str(ei.value) and "ab12" not in str(ei.value)


def test_b02_nonzero_exit_via_subprocess(prod, monkeypatch):
    monkeypatch.setattr(
        ba.subprocess, "run",
        _CapRun(stdout="", returncode=2,
                stderr="打开分享失败 %s pwd=ab12" % SHARE))
    with pytest.raises(AdapterError) as ei:
        prod.list_share_page(SHARE, "ab12", None, None, 100)
    assert ei.value.code == "connector_failed"
    assert SHARE not in str(ei.value) and "ab12" not in str(ei.value)


def test_b02_unknown_json_shapes_fail(prod, monkeypatch):
    # 未知 JSON / 缺 has_more（缺页）/ fs_id 为 float → 显式失败，不当空分享
    bad_payloads = [
        '{"foo": 1}',                                  # 未知结构
        '[]',                                          # 顶层缺页信息
        'not json at all',
        json.dumps({"list": [{"fs_id": "1", "server_filename": "a.svs",
                              "size": 1, "isdir": False}]}),  # 缺 has_more
        json.dumps({"list": [{"fs_id": 1.5, "server_filename": "a.svs",
                              "size": 1, "isdir": False}],
                    "has_more": False}),               # float fs_id
        json.dumps({"list": [{"fs_id": "1", "server_filename": "a.svs",
                              "size": -3, "isdir": False}],
                    "has_more": False}),               # 负 size
        json.dumps({"list": [{"fs_id": "1", "size": 1, "isdir": "yes"}],
                    "has_more": False}),               # isdir 非 bool
    ]
    for payload in bad_payloads:
        monkeypatch.setattr(prod, "_run", lambda *a, **k: payload)
        with pytest.raises(AdapterError) as ei:
            prod.list_share_page(SHARE, None, None, None, 100)
        assert ei.value.code == "connector_output_invalid", payload


def test_b02_download_rejects_share_url_and_traversal(prod, monkeypatch):
    monkeypatch.setattr(prod, "_run", lambda *a, **k: "{}")
    for bad in (SHARE, "/etc/passwd", "../escape.svs",
                "bib_demo/../other.svs", "bib_demo"):
        with pytest.raises(AdapterError) as ei:
            prod.download_to(bad, "/tmp/dest")
        assert ei.value.code == "cleanup_path_rejected"


def test_b02_cleanup_rejects_foreign_paths(prod, monkeypatch):
    monkeypatch.setattr(prod, "_run", lambda *a, **k: "{}")
    with pytest.raises(AdapterError) as ei:
        prod.cleanup_batch_copies("bib_demo", ["otherbatch/x.svs"])
    assert ei.value.code == "cleanup_path_rejected"
    with pytest.raises(AdapterError) as ei2:
        prod.cleanup_batch_copies("bib_demo", ["bib_demo/../../etc"])
    assert ei2.value.code == "cleanup_path_rejected"


# --------------------------------------------------------------------------- #
# capabilities（reason_code；探测用本地无副作用二进制）
# --------------------------------------------------------------------------- #

def _caps(monkeypatch, bin_path, enum_on="true", import_on="true",
          secret="k"):
    monkeypatch.setenv("BAIDU_ENUMERATION_ENABLED", enum_on)
    monkeypatch.setenv("BAIDU_IMPORT_ENABLED", import_on)
    monkeypatch.setenv("BAIDU_SHARE_SECRET_KEY", secret)
    monkeypatch.setenv("BAIDU_CONNECTOR_BIN", bin_path)
    return ProductionBaiduAdapter().capabilities()


def test_b02_capabilities_reason_codes(monkeypatch):
    caps = _caps(monkeypatch, "/bin/true")
    assert caps["enumeration_available"] and caps["import_available"]
    assert caps["reason_code"] is None
    assert caps["limits"]["max_entries"] == 10000
    # 探测失败（exit != 0）→ connector_unusable
    assert _caps(monkeypatch, "/bin/false")["reason_code"] == \
        "connector_unusable"
    # 二进制缺失 → connector_missing
    assert _caps(monkeypatch, "/nonexistent/bdpan")["reason_code"] == \
        "connector_missing"
    # 开关关闭 → 独立 reason（枚举关 / 导入关）
    assert _caps(monkeypatch, "/bin/true", enum_on="false")["reason_code"] \
        == "enumeration_disabled"
    caps2 = _caps(monkeypatch, "/bin/true", import_on="false")
    assert caps2["import_available"] is False
    assert caps2["reason_code"] == "import_disabled"
    assert caps2["enumeration_available"] is True
    # 缺密钥 → secret_unconfigured（优先级最高）
    assert _caps(monkeypatch, "/bin/true", secret="")["reason_code"] == \
        "secret_unconfigured"


# --------------------------------------------------------------------------- #
# fake 适配器基础行为（分页/重复游标/上限溢出由 store 侧 B03 验证）
# --------------------------------------------------------------------------- #

def test_fake_pagination_and_counters():
    fake = FakeBaiduAdapter(
        entries=[{"path": "/a%d.svs" % i, "size": i} for i in range(5)],
        page_size=2)
    p1 = fake.list_share_page(SHARE, None, "", None, 100)
    assert len(p1["items"]) == 2 and p1["has_more"] is True \
        and p1["next_cursor"] == "2"
    p2 = fake.list_share_page(SHARE, None, "", "2", 100)
    p3 = fake.list_share_page(SHARE, None, "", "3", 100)
    assert len(p3["items"]) == 1 and p3["has_more"] is False
    assert p2["items"][0]["fs_id"] != p3["items"][0]["fs_id"]


def test_fake_download_writes_fixture_bytes(tmp_path):
    fake = FakeBaiduAdapter(
        entries=[{"path": "/x.svs", "size": 7, "content": b"bytes42"}])
    out = fake.transfer_selected("bib_t", SHARE, None,
                                 [fake._files["x.svs"]["fs_id"]])
    assert fake.poll_transfer(out["task_id"])["state"] == "succeeded"
    fake.download_to("bib_t/x.svs", tmp_path)
    assert (tmp_path / "x.svs").read_bytes() == b"bytes42"
    assert fake.counters() == {"list": 0, "transfer": 1, "download": 1,
                               "delete": 0, "probe": 0}


def test_get_adapter_env_injection(monkeypatch):
    monkeypatch.delenv("BAIDU_ADAPTER", raising=False)
    assert isinstance(get_adapter(), ProductionBaiduAdapter)
    monkeypatch.setenv("BAIDU_ADAPTER", "fake")
    a = get_adapter()
    b = get_adapter()
    assert isinstance(a, FakeBaiduAdapter) and a is b  # 单例保计数器
    # fake 通道在生产环境标记下显式不可用（防测试通道漏进生产）
    monkeypatch.setenv("PT_ENV", "production")
    caps = a.capabilities()
    assert caps["enumeration_available"] is False
    assert caps["reason_code"] == "fake_adapter_forbidden_in_production"
