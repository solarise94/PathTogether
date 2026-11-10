# -*- coding: utf-8 -*-
"""cos_client.list_parts 的客户端选择（upload-resilience 复核 2026-11-10）。

背景：SDK 默认 30s 超时 × (retry=3 → 4 次尝试) 会把 web 线程钉住约 2 分钟，
而浏览器 3 秒就放弃。约定：

  - worker 调用不传 timeout/retry → 仍用默认客户端（行为逐字节不变）；
  - web 调用传 timeout/retry → 独立缓存的受限客户端，缓存键包含两者。

SDK 类在 ``cos_client`` 模块层被替换为假实现（不连真实 COS）。
"""

import cos_client
import cos_config


def _install_fake_sdk(monkeypatch, parts=None):
    calls = {"config": [], "client": []}

    class FakeConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls["config"].append(kwargs)

    class FakeClient:
        def __init__(self, conf, retry=3):
            self.conf = conf
            self.retry = retry
            calls["client"].append({"conf": conf.kwargs, "retry": retry})

        def list_parts(self, **kwargs):
            return {"Part": list(parts or []), "IsTruncated": "false"}

    monkeypatch.setattr(cos_client, "CosConfig", FakeConfig)
    monkeypatch.setattr(cos_client, "CosS3Client", FakeClient)
    monkeypatch.setattr(cos_client, "_client_cache", {})
    monkeypatch.setattr(cos_config, "COS_BUCKET", "bucket-1250000000")
    monkeypatch.setattr(cos_config, "COS_REGION", "ap-test")
    monkeypatch.setattr(cos_config, "cos_credentials",
                        lambda: ("sid", "skey", True))
    return calls


def test_list_parts_default_path_uses_default_client(monkeypatch):
    """worker 路径：不传参数 → 默认客户端（无 Timeout 覆盖，retry SDK 默认）。"""
    calls = _install_fake_sdk(monkeypatch)
    assert cos_client.list_parts("incoming/u/job/x", "up-1") == []
    assert len(calls["client"]) == 1
    assert "Timeout" not in calls["config"][0]
    assert calls["client"][0]["retry"] == 3
    # 默认缓存键保持原样（worker 与 web 客户端互不覆盖）
    assert "bucket-1250000000|ap-test|sid" in cos_client._client_cache


def test_list_parts_bounded_client_uses_web_settings(monkeypatch):
    calls = _install_fake_sdk(monkeypatch, parts=[
        {"PartNumber": 2, "ETag": '"bb"', "Size": 10,
         "LastModified": "2026-11-10T00:00:00.000Z"},
        {"PartNumber": 1, "ETag": '"aa"', "Size": 10},
    ])
    parts = cos_client.list_parts("incoming/u/job/x", "up-1",
                                  timeout=3, retry=0)
    assert [p["part_number"] for p in parts] == [1, 2]
    assert calls["config"][0]["Timeout"] == 3
    assert calls["client"][0]["retry"] == 0
    # 缓存键包含受限参数：与默认客户端隔离
    assert any("timeout=3" in k and "retry=0" in k
               for k in cos_client._client_cache)
    assert "bucket-1250000000|ap-test|sid" not in cos_client._client_cache

    # 相同设置复用同一客户端；不同设置新建一个
    cos_client.list_parts("incoming/u/job/x", "up-1", timeout=3, retry=0)
    assert len(calls["client"]) == 1
    cos_client.list_parts("incoming/u/job/x", "up-1", timeout=8, retry=1)
    assert len(calls["client"]) == 2
    assert calls["config"][1]["Timeout"] == 8
    assert calls["client"][1]["retry"] == 1


def test_list_parts_default_and_bounded_clients_coexist(monkeypatch):
    """worker 默认客户端与 web 受限客户端各自缓存，互不替换。"""
    calls = _install_fake_sdk(monkeypatch)
    cos_client.list_parts("incoming/u/job/x", "up-1")
    cos_client.list_parts("incoming/u/job/x", "up-1", timeout=8, retry=1)
    cos_client.list_parts("incoming/u/job/x", "up-1")
    assert len(calls["client"]) == 2
    assert calls["client"][0]["retry"] == 3
    assert calls["client"][1]["retry"] == 1
