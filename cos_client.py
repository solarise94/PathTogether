# -*- coding: utf-8 -*-
"""COS 客户端薄适配（产品化，COS 直传，裁决 A-presign-parts）。

**协议层全部走官方 ``cos-python-sdk-v5``**（docs/upload-routing-open-source-review.md
§3 组件复用原则 2026-09-25：PoC 的手写签名只用于实验，生产不自研签名算法）。
本项目代码只做：配置注入、接口形状归一（worker/控制 API 依赖的稳定函数签名）、
异常脱敏（任何错误消息不得携带签名 URL/SecretId/SecretKey）。

官方 SDK 已在专用测试桶重新验收（docs/evidence/cos-20260924.md §10.6）：

- 预签名绑定 Content-Length：精确长度 200；超长/截短/改 partNumber 均
  403 SignatureDoesNotMatch；方法改用 400；过期重放 403；
- Complete 响应含 ``x-cos-version-id``；HEAD 暴露版本与 Content-Length；
- Range GET 返回 206 语义的 Content-Range，流式读取；
- 版本列表/进行中 multipart 分页字段如本适配所映射。

分工边界（合同 §3.1/§4）：

- **控制 API（app.py）只调用 ``presign_upload_part``**——纯本地计算，无网络；
- **一切网络调用仅由 cos_ingest_worker.py 发起**。
"""

from __future__ import annotations

import threading
import urllib.parse
from typing import Dict, List, Optional, Tuple

import cos_config

from qcloud_cos import CosConfig, CosS3Client
from qcloud_cos.cos_exception import CosClientError as _SdkClientError
from qcloud_cos.cos_exception import CosServiceError as _SdkServiceError


class CosAdapterError(Exception):
    """COS 调用失败（消息已脱敏：只含 method/key 形状/HTTP 状态/错误码）。"""


class CosConfigMissing(CosAdapterError):
    """bucket/region/凭证缺失（blocked_external_input，调用方空转告警）。"""


# 兼容别名（Phase 2 worker 以 CosClientError 捕获）
CosClientError = CosAdapterError

_client_lock = threading.Lock()
_client_cache: Dict[str, CosS3Client] = {}


def _client() -> CosS3Client:
    sid, skey, ok = cos_config.cos_credentials()
    if not ok:
        raise CosConfigMissing(
            "COS 配置缺失（COS_BUCKET/COS_REGION/COS_SECRET_ID/COS_SECRET_KEY）"
            "——调用方应空转告警，capability 保持 off")
    cache_key = "%s|%s|%s" % (cos_config.COS_BUCKET, cos_config.COS_REGION, sid)
    with _client_lock:
        cli = _client_cache.get(cache_key)
        if cli is None:
            cli = CosS3Client(CosConfig(
                Region=cos_config.COS_REGION, SecretId=sid, SecretKey=skey,
                Scheme="https"))
            _client_cache[cache_key] = cli
        return cli


def _bucket() -> str:
    if not cos_config.COS_BUCKET:
        raise CosConfigMissing("COS_BUCKET 未配置")
    return cos_config.COS_BUCKET


def _key_shape(key: str) -> str:
    """key 形状脱敏（保留前两级目录，余下折叠——不泄露完整对象名也够排障）。"""
    parts = key.split("/")
    return "/".join(parts[:3]) + ("/…%d段" % len(parts) if len(parts) > 3 else "")


def _wrap(exc, method, key) -> CosAdapterError:
    if isinstance(exc, _SdkServiceError):
        return CosAdapterError("COS %s %s 失败：HTTP %s %s" % (
            method, _key_shape(key), exc.get_status_code(), exc.get_error_code()))
    return CosAdapterError("COS %s %s 客户端错误：%s" % (
        method, _key_shape(key), str(exc)[:120]))


def redact_url(url: str) -> str:
    """日志脱敏：签名参数一律 <redacted>（预签名 URL 不得整体进日志）。"""
    parsed = urllib.parse.urlsplit(url)
    kept = []
    for part in parsed.query.split("&"):
        name = part.split("=", 1)[0]
        if name in ("q-ak", "q-signature", "q-sign-time", "q-key-time",
                    "x-cos-security-token"):
            kept.append(f"{name}=<redacted>")
        else:
            kept.append(f"{name}=<value>")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "&".join(kept), ""))


# --------------------------------------------------------------------------- #
# multipart（worker 专用；浏览器只拿 presign_upload_part 的产物）
# --------------------------------------------------------------------------- #
def initiate_multipart(key: str) -> str:
    try:
        return _client().create_multipart_upload(
            Bucket=_bucket(), Key=key)["UploadId"]
    except (_SdkClientError, _SdkServiceError, KeyError) as e:
        raise _wrap(e, "POST ?uploads", key) from e


def presign_upload_part(key: str, upload_id: str, part_number: int,
                        content_length: int,
                        ttl_seconds: Optional[int] = None) -> Dict[str, object]:
    """绑定 Content-Length 的 UploadPart 预签名（A 方案唯一浏览器授权形态）。

    纯本地计算无网络；不绑定长度的 URL 生产禁止签发（§7.2：不绑定时 COS
    接受超长分块）。返回 {"url","part_number","content_length","expires_in"}。
    """
    url = _client().get_presigned_url(
        _bucket(), key, "PUT",
        Expired=(cos_config.COS_PART_URL_TTL_SECONDS
                 if ttl_seconds is None else int(ttl_seconds)),
        Params={"partNumber": str(int(part_number)),
                "uploadId": upload_id},
        Headers={"Content-Length": str(int(content_length))})
    # 防御：SDK 升级若改变行为（长度/参数未入签名），宁可拒绝签发
    q = urllib.parse.urlsplit(url).query.lower()
    if "content-length" not in q or "partnumber=" not in q \
            or "uploadid=" not in q:
        raise CosAdapterError("预签名自检失败：长度/partNumber/uploadId 未入签名")
    return {"url": url, "part_number": int(part_number),
            "content_length": int(content_length)}


def list_parts(key: str, upload_id: str) -> List[Dict[str, object]]:
    """分页列举已上传分块 → [{part_number, etag, size}]（编号升序）。"""
    try:
        parts: List[Dict[str, object]] = []
        marker = 0
        while True:
            resp = _client().list_parts(
                Bucket=_bucket(), Key=key, UploadId=upload_id,
                PartNumberMarker=marker)
            for p in (resp.get("Part") or []):
                parts.append({"part_number": int(p["PartNumber"]),
                              "etag": p["ETag"], "size": int(p["Size"])})
            if str(resp.get("IsTruncated", "false")).lower() != "true":
                break
            marker = int(resp.get("NextPartNumberMarker", 0) or 0)
        parts.sort(key=lambda p: p["part_number"])
        return parts
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "GET ?uploadId (ListParts)", key) from e


def complete_multipart(key: str, upload_id: str,
                       parts: List[Dict[str, object]]) -> Dict[str, str]:
    """Complete（worker 唯一持有）；返回 {version_id, etag}。

    版本化桶下必须取得 x-cos-version-id（钉源依据，§3.1）；缺失视为失败。
    """
    try:
        resp = _client().complete_multipart_upload(
            Bucket=_bucket(), Key=key, UploadId=upload_id,
            MultipartUpload={"Part": [
                {"PartNumber": int(p["part_number"]), "ETag": p["etag"]}
                for p in sorted(parts, key=lambda x: x["part_number"])]})
        version = resp.get("x-cos-version-id") or ""
        if not version:
            raise CosAdapterError(
                "Complete 未返回 x-cos-version-id（桶未开版本控制？）")
        return {"version_id": version, "etag": resp.get("ETag") or ""}
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "POST ?uploadId (Complete)", key) from e


def abort_multipart(key: str, upload_id: str) -> None:
    try:
        _client().abort_multipart_upload(
            Bucket=_bucket(), Key=key, UploadId=upload_id)
    except _SdkServiceError as e:
        if e.get_error_code() == "NoSuchUpload":
            return  # 幂等：已关闭
        raise _wrap(e, "DELETE ?uploadId (Abort)", key) from e
    except _SdkClientError as e:
        raise _wrap(e, "DELETE ?uploadId (Abort)", key) from e


# --------------------------------------------------------------------------- #
# 对象读取/删除（worker 专用）
# --------------------------------------------------------------------------- #
def head_object(key: str, version_id: Optional[str] = None) -> Dict[str, str]:
    kwargs = {"Bucket": _bucket(), "Key": key}
    if version_id:
        kwargs["VersionId"] = version_id
    try:
        resp = _client().head_object(**kwargs)
        return {"size": str(resp.get("Content-Length") or ""),
                "etag": resp.get("ETag") or "",
                "version_id": resp.get("x-cos-version-id") or version_id or ""}
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "HEAD", key) from e


class _StreamWrap:
    """把 SDK 的 dict+StreamBody 归一成 urllib 风格流式响应
    （.status_code / .headers / .read(n)）。Range 的 206/Content-Range
    校验仍由调用方执行（§3.3）。"""

    def __init__(self, resp):
        code = int(resp.get("StatusCode") or
                   (206 if resp.get("Content-Range") else 200))
        # .status 为 urllib 风格别名（worker 按此读取）；.status_code 同值
        self.status = code
        self.status_code = code
        self.headers = {str(k): str(v) for k, v in resp.items()
                        if isinstance(k, str)}
        self._iter = resp["Body"].get_stream()
        self._buf = b""

    def read(self, n: Optional[int] = None) -> bytes:
        while n is None or len(self._buf) < n:
            try:
                self._buf += next(self._iter)
            except StopIteration:
                break
        if n is None:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self):  # 迭代耗尽即释放；无显式资源
        pass


def get_object(key: str, version_id: str, range_header: Optional[str] = None,
               timeout: int = 120):
    """版本化 GET（流式）。"""
    kwargs: Dict[str, object] = {"Bucket": _bucket(), "Key": key,
                                 "VersionId": version_id}
    if range_header:
        kwargs["Range"] = range_header
    try:
        return _StreamWrap(_client().get_object(**kwargs))
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "GET", key) from e


def delete_object_version(key: str, version_id: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=key, VersionId=version_id)
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "DELETE", key) from e


# --------------------------------------------------------------------------- #
# 对账分页列举（reconciler；只读）。marker 为 "key|version-or-uploadId" 复合串，
# 调用方当不透明字符串回传即可。
# --------------------------------------------------------------------------- #
def list_object_versions_page(prefix: str, key_marker: str = ""
                              ) -> Tuple[List[Dict[str, object]], bool, str]:
    kwargs: Dict[str, object] = {"Bucket": _bucket(), "Prefix": prefix,
                                 "MaxKeys": 1000}
    if key_marker and "|" in key_marker:
        k, v = key_marker.split("|", 1)
        kwargs.update({"KeyMarker": k, "VersionIdMarker": v})
    try:
        resp = _client().list_objects_versions(**kwargs)
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "GET ?versions", prefix) from e
    items = [{
        "key": v["Key"], "version_id": v["VersionId"],
        "is_latest": str(v.get("IsLatest", "false")).lower() == "true",
        "size": int(v.get("Size", 0) or 0), "is_delete_marker": False,
    } for v in (resp.get("Version") or [])]
    items += [{
        "key": d["Key"], "version_id": d["VersionId"],
        "is_latest": str(d.get("IsLatest", "false")).lower() == "true",
        "size": 0, "is_delete_marker": True,
    } for d in (resp.get("DeleteMarker") or [])]
    truncated = str(resp.get("IsTruncated", "false")).lower() == "true"
    next_marker = "|".join([resp.get("NextKeyMarker", ""),
                            resp.get("NextVersionIdMarker", "")]) \
        if truncated else ""
    return items, truncated, next_marker


def list_multipart_uploads_page(prefix: str, key_marker: str = ""
                                ) -> Tuple[List[Dict[str, object]], bool, str]:
    kwargs: Dict[str, object] = {"Bucket": _bucket(), "Prefix": prefix}
    if key_marker and "|" in key_marker:
        k, u = key_marker.split("|", 1)
        kwargs.update({"KeyMarker": k, "UploadIdMarker": u})
    try:
        resp = _client().list_multipart_uploads(**kwargs)
    except (_SdkClientError, _SdkServiceError) as e:
        raise _wrap(e, "GET ?uploads", prefix) from e
    items = [{
        "key": u["Key"], "upload_id": u["UploadId"],
        "initiated": str(u.get("Initiated", "")),
    } for u in (resp.get("Upload") or [])]
    truncated = str(resp.get("IsTruncated", "false")).lower() == "true"
    next_marker = "|".join([resp.get("NextKeyMarker", ""),
                            resp.get("NextUploadIdMarker", "")]) \
        if truncated else ""
    return items, truncated, next_marker
