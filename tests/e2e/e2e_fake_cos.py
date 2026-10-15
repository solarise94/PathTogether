# -*- coding: utf-8 -*-
"""进程内假 COS（E2E 用；只被 tests/e2e/e2e_server.py 在 E2E_INLINE_CONVERT=1 时装配）。

存在理由（docs/upload-inline-convert-design-20261015.md §9.4「为让 A 的
『上传字节被平台读取器打开』真成立」）：真实 Chromium 要把本机转换产物
PUT 到预签名 URL、服务端 worker 要把同一批字节下载回来用真实
``slide_io.open_slide`` 打开——两端都得有一个真的对象存储端点。

实现选择（相对设计稿的一处收紧，已实测）：

- **不做 TLS**：设计稿写「本地 HTTPS + 自签证书 + --ignore-certificate-errors」。
  实测更省事且语义等价的做法是 HTTP——预签名 URL 的 scheme 由客户端
  ``CosConfig(Scheme=...)`` 决定（``cos_client._client()`` 在
  ``_client_cache`` 未命中时才构造，测试注入点即由此而来），HTTP 则不需要
  自签证书、不需要 Chromium 关证书校验、也不需要 host-resolver 映射；
  scheme 与「字节真的经过网络往返」这一被测语义无关。
- **独立端口 + 显式 CORS**：预签名 URL 走 path-style
  （``http://127.0.0.1:<port>/incoming/<owner>/<job>/<rand>``）——实测
  ``CosConfig(Domain="127.0.0.1:<port>")`` 是唯一产出正确主机的旋钮
  （``Endpoint=`` 会拼出 ``<bucket>.127.0.0.1:9443`` 这类坏主机，设计稿
  §10.16 已记录）。浏览器侧 XHR 因此是**跨源** PUT（页面在
  ``127.0.0.1:<主端口>``），必须回 CORS 头并放行预检（``File.slice`` 继承
  MIME，浏览器会附带 Content-Type → 非简单请求）。
- 桶级操作（ListParts/ListObjectVersions/ListMultipartUploads/Initiate/
  Complete/Abort/HEAD/DELETE）全部由 ``qcloud_cos`` SDK 在**服务端进程内**
  调用（worker duties），只有分块 PUT 来自浏览器。

覆盖的 COS REST 子集（与 ``cos_client.py`` 的调用面一一对应）：

    POST   /<key>?uploads=             InitiateMultipartUpload
    PUT    /<key>?partNumber=&uploadId= UploadPart（浏览器 XHR）
    GET    /<key>?uploadId=            ListParts（分页 marker）
    POST   /<key>?uploadId=            CompleteMultipartUpload
    DELETE /<key>?uploadId=            AbortMultipartUpload（幂等）
    HEAD   /<key>[?versionId=]         HEAD 对象（Content-Length/ETag/版本）
    GET    /<key>[?versionId=][Range]  GET 对象（206 + Content-Range）
    DELETE /<key>?versionId=           删版本
    GET    /?versions=&prefix=         ListObjectVersions（分页）
    GET    /?uploads=&prefix=          ListMultipartUploads（分页）

版本语义与生产一致：对象按版本保存，Complete 返回 ``x-cos-version-id``
（``cos_client.complete_multipart`` 缺版本即失败——生产桶开了版本控制，
假桶必须同形，否则该分支永远测不到）。

不写任何仓库内路径：对象字节只在该进程内存里（E2E 进程退出即消失）。
"""
from __future__ import annotations

import itertools
import threading
import urllib.parse
import xml.sax.saxutils as _sax

from flask import Flask, Response, request

#: 假 bucket（与 e2e_server 下发的 COS_BUCKET 一致；只是签名材料，不参与路由）
BUCKET = "e2ecos-1250000000"
REGION = "ap-e2e"
SECRET_ID = "AKIDe2ee2e"
SECRET_KEY = "e2e-secret-key-000000"


def _xml(body: str) -> Response:
    return Response('<?xml version="1.0" encoding="UTF-8"?>' + body,
                    status=200, mimetype="application/xml")


def _esc(value) -> str:
    return _sax.escape(str(value))


class FakeCos:
    """内存桶：``objects[key] -> [{version_id, data, delete_marker}]``、
    ``uploads[key] -> {upload_id: {part_number: bytes}}``。

    线程安全（Flask threaded + 浏览器并发分块 PUT）：全部状态访问走同一把锁。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = itertools.count(1)
        self.objects: dict[str, list[dict]] = {}
        self.uploads: dict[str, dict[str, dict[int, bytes]]] = {}
        #: 浏览器实际 PUT 上来的分块（key → {part_number: bytes}），Complete
        #: 之后**保留**——测试要「把上传的分块拼回」来核对，而 Complete 会
        #: 消耗 uploadId（ListParts 随之 404）。同一编号被重传时以后者覆盖
        #: （与 COS 的 UploadPart 覆盖语义一致）。
        self.received: dict[str, dict[int, bytes]] = {}

    # ------------------------------------------------------------ helpers --
    def _vid(self, prefix: str) -> str:
        return "%s-%d" % (prefix, next(self._seq))

    def _latest(self, key):
        with self._lock:
            versions = self.objects.get(key) or []
            for v in reversed(versions):
                if not v["delete_marker"]:
                    return dict(v)
        return None

    def _by_version(self, key, version_id):
        with self._lock:
            for v in self.objects.get(key) or []:
                if v["version_id"] == version_id:
                    return dict(v)
        return None

    # ----------------------------------------------------------- REST app --
    def app(self) -> Flask:
        app = Flask("e2e-fake-cos")
        app.config["MAX_CONTENT_LENGTH"] = None      # 分块 PUT 由签名约束
        app.logger.disabled = True
        app.add_url_rule("/", "root", self._dispatch,
                         methods=["GET", "POST", "PUT", "HEAD", "DELETE", "OPTIONS"])
        app.add_url_rule("/<path:key>", "object", self._dispatch,
                         methods=["GET", "POST", "PUT", "HEAD", "DELETE", "OPTIONS"])
        return app

    # CORS：浏览器分块 PUT 是跨源（页面在另一个端口）。预检必须放行
    # Content-Type（File.slice 继承 MIME），PUT 响应必须暴露 ETag
    # （cos-uploader 用响应头 ETag 作续传提示）。
    def _cors(self, resp: Response) -> Response:
        resp.headers["Access-Control-Allow-Origin"] = \
            request.headers.get("Origin") or "*"
        resp.headers["Access-Control-Allow-Methods"] = "PUT, GET, HEAD, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "ETag, x-cos-version-id"
        resp.headers["Vary"] = "Origin"
        return resp

    def _dispatch(self, key: str = ""):
        if request.method == "OPTIONS":
            resp = Response("", status=204)
            resp.headers["Access-Control-Allow-Headers"] = \
                request.headers.get("Access-Control-Request-Headers") or \
                "content-type"
            resp.headers["Access-Control-Max-Age"] = "600"
            return self._cors(resp)
        handler = getattr(self, "_m_" + request.method.lower())
        try:
            resp = handler(key)
        except Exception as exc:  # noqa: BLE001 — 测试端点：500 也要带 CORS
            resp = Response("fake-cos error: %r" % (exc,), status=500,
                            mimetype="text/plain")
        return self._cors(resp)

    # --------------------------------------------------------------- ops --
    def _m_post(self, key):
        q = request.args
        if "uploads" in q:
            with self._lock:
                uid = self._vid("up")
                self.uploads.setdefault(key, {})[uid] = {}
            return _xml("<InitiateMultipartUploadResult><Bucket>%s</Bucket>"
                        "<Key>%s</Key><UploadId>%s</UploadId>"
                        "</InitiateMultipartUploadResult>"
                        % (_esc(BUCKET), _esc(key), _esc(uid)))
        upload_id = q.get("uploadId") or ""
        with self._lock:
            parts = (self.uploads.get(key) or {}).pop(upload_id, None)
            if parts is None:
                # 生产语义：uploadId 已消耗/不存在 → NoSuchUpload（Complete
                # 恢复分支按此判定），不是 200
                return Response(
                    "<Error><Code>NoSuchUpload</Code></Error>", status=404,
                    mimetype="application/xml")
            data = b"".join(parts[n] for n in sorted(parts))
            vid = self._vid("ver")
            self.objects.setdefault(key, []).append(
                {"version_id": vid, "data": data, "delete_marker": False})
        resp = _xml("<CompleteMultipartUploadResult><Location>/%s</Location>"
                    "<Bucket>%s</Bucket><Key>%s</Key><ETag>\"fin-%s\"</ETag>"
                    "</CompleteMultipartUploadResult>"
                    % (_esc(key), _esc(BUCKET), _esc(key), _esc(vid)))
        resp.headers["x-cos-version-id"] = vid
        return resp

    def _m_put(self, key):
        q = request.args
        number = int(q.get("partNumber") or 0)
        upload_id = q.get("uploadId") or ""
        body = request.get_data()
        with self._lock:
            slot = self.uploads.setdefault(key, {}).setdefault(upload_id, {})
            slot[number] = body
            self.received.setdefault(key, {})[number] = body
        resp = Response("", status=200, mimetype="text/plain")
        resp.headers["ETag"] = '"etag-%d-%d"' % (number, len(body))
        return resp

    def received_parts(self, key: str) -> dict:
        """浏览器 PUT 上来的分块快照 ``{part_number: bytes}``（Complete 后仍在）。"""
        with self._lock:
            return dict(self.received.get(key) or {})

    def _m_get(self, key):
        q = request.args
        # 桶级列举：ListObjectVersions / ListMultipartUploads
        if not key:
            if "versions" in q:
                return self._list_versions(q)
            if "uploads" in q:
                return self._list_uploads(q)
            return Response("<Error><Code>NoSuchBucket</Code></Error>",
                            status=404, mimetype="application/xml")
        if "uploadId" in q:
            return self._list_parts(key, q)
        if "uploads" in q:
            return self._list_uploads(q)
        obj = None
        version_id = q.get("versionId")
        obj = (self._by_version(key, version_id) if version_id
               else self._latest(key))
        if obj is None:
            return Response("<Error><Code>NoSuchKey</Code></Error>", status=404,
                            mimetype="application/xml")
        return self._object_body(obj, request.headers.get("Range"))

    def _object_body(self, obj, range_header):
        data = obj["data"]
        if range_header:
            try:
                first, last = range_header[len("bytes="):].split("-")[:2]
                start, end = int(first), int(last)
            except ValueError:
                return Response("bad range", status=416, mimetype="text/plain")
            if start >= len(data):
                return Response("", status=416, mimetype="text/plain")
            end = min(end, len(data) - 1)
            chunk = data[start:end + 1]
            resp = Response(chunk, status=206, mimetype="application/octet-stream")
            resp.headers["Content-Range"] = "bytes %d-%d/%d" % (
                start, end, len(data))
            resp.headers["Content-Length"] = str(len(chunk))
            return resp
        return Response(data, status=200, mimetype="application/octet-stream")

    def _m_head(self, key):
        obj = None
        version_id = request.args.get("versionId")
        obj = (self._by_version(key, version_id) if version_id
               else self._latest(key))
        if obj is None:
            return Response("", status=404)
        resp = Response("", status=200, mimetype="application/octet-stream")
        resp.headers["Content-Length"] = str(len(obj["data"]))
        resp.headers["ETag"] = '"obj-%s"' % obj["version_id"]
        resp.headers["x-cos-version-id"] = obj["version_id"]
        return resp

    def _m_delete(self, key):
        q = request.args
        if "uploadId" in q:
            with self._lock:
                self.uploads.get(key, {}).pop(q["uploadId"], None)
            return Response("", status=204)      # Abort 幂等
        version_id = q.get("versionId")
        with self._lock:
            versions = self.objects.get(key) or []
            if version_id:
                self.objects[key] = [v for v in versions
                                     if v["version_id"] != version_id]
            else:
                self.objects[key] = []
        return Response("", status=204)

    # ------------------------------------------------------------ listings --
    def _paged(self, items, marker, marker_key, arg, max_key):
        limit = int(request.args.get(max_key) or 1000)
        page = [i for i in items if not marker or i[marker_key] > marker]
        truncated = len(page) > limit
        return page[:limit], truncated

    def _list_versions(self, q):
        prefix = q.get("prefix") or ""
        with self._lock:
            rows = []
            for key in sorted(self.objects):
                if not key.startswith(prefix):
                    continue
                for v in self.objects[key]:
                    rows.append({"key": key, "version_id": v["version_id"],
                                 "is_latest": True,
                                 "size": 0 if v["delete_marker"] else len(v["data"]),
                                 "delete_marker": v["delete_marker"]})
        marker = q.get("key-marker") or ""
        page, truncated = self._paged(rows, marker, "key", "key-marker",
                                      "max-keys")
        body = []
        for r in page:
            tag = "DeleteMarker" if r["delete_marker"] else "Version"
            body.append("<%s><Key>%s</Key><VersionId>%s</VersionId>"
                        "<IsLatest>true</IsLatest><Size>%d</Size></%s>"
                        % (tag, _esc(r["key"]), _esc(r["version_id"]),
                           r["size"], tag))
        next_key = page[-1]["key"] if (truncated and page) else ""
        return _xml("<ListVersionsResult><IsTruncated>%s</IsTruncated>"
                    "<NextKeyMarker>%s</NextKeyMarker>%s</ListVersionsResult>"
                    % ("true" if truncated else "false", _esc(next_key),
                       "".join(body)))

    def _list_uploads(self, q):
        prefix = q.get("prefix") or ""
        with self._lock:
            rows = [{"key": key, "upload_id": uid}
                    for key in sorted(self.uploads)
                    if key.startswith(prefix)
                    for uid in sorted(self.uploads[key])]
        marker = q.get("key-marker") or ""
        page, truncated = self._paged(rows, marker, "key", "key-marker",
                                      "max-uploads")
        body = "".join(
            "<Upload><Key>%s</Key><UploadId>%s</UploadId>"
            "<Initiated>2026-01-01T00:00:00.000Z</Initiated></Upload>"
            % (_esc(r["key"]), _esc(r["upload_id"])) for r in page)
        next_key = page[-1]["key"] if (truncated and page) else ""
        return _xml("<ListMultipartUploadsResult><IsTruncated>%s</IsTruncated>"
                    "<NextKeyMarker>%s</NextKeyMarker>%s"
                    "</ListMultipartUploadsResult>"
                    % ("true" if truncated else "false", _esc(next_key), body))

    def _list_parts(self, key, q):
        upload_id = q["uploadId"]
        with self._lock:
            parts = dict((self.uploads.get(key) or {}).get(upload_id) or {})
        if not parts and upload_id not in (self.uploads.get(key) or {}):
            return Response("<Error><Code>NoSuchUpload</Code></Error>",
                            status=404, mimetype="application/xml")
        marker = int(q.get("part-number-marker") or 0)
        limit = int(q.get("max-parts") or 1000)
        numbers = sorted(n for n in parts if n > marker)
        page = numbers[:limit]
        truncated = len(numbers) > limit
        body = "".join(
            "<Part><PartNumber>%d</PartNumber><ETag>\"etag-%d-%d\"</ETag>"
            "<Size>%d</Size></Part>" % (n, n, len(parts[n]), len(parts[n]))
            for n in page)
        next_marker = page[-1] if (truncated and page) else 0
        return _xml("<ListPartsResult><Bucket>%s</Bucket><Key>%s</Key>"
                    "<UploadId>%s</UploadId><IsTruncated>%s</IsTruncated>"
                    "<NextPartNumberMarker>%d</NextPartNumberMarker>%s"
                    "</ListPartsResult>"
                    % (_esc(BUCKET), _esc(key), _esc(upload_id),
                       "true" if truncated else "false", next_marker, body))


# --------------------------------------------------------------------------- #
# 测试侧装配：起一个线程内的共享端口，并把 cos_client 指向它
# --------------------------------------------------------------------------- #
def start(capacity_bytes=10_000_000_000, safety_bytes=100_000_000):
    """起假 COS（随机空闲端口，daemon 线程，随进程退出）。

    返回 ``(fake, port, url)``。同时把进程环境变量配成假值（**不含任何真
    凭证**）——``cos_config`` 在模块导入期读 env，故本函数必须在
    ``import app`` 之前调用。
    """
    import os
    import socket

    os.environ["COS_BUCKET"] = BUCKET
    os.environ["COS_REGION"] = REGION
    os.environ["COS_SECRET_ID"] = SECRET_ID
    os.environ["COS_SECRET_KEY"] = SECRET_KEY

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    fake = FakeCos()
    app = fake.app()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True),
        name="e2e-fake-cos", daemon=True).start()
    with _REGISTRY_LOCK:
        _REGISTRY[port] = fake
    return fake, port, "http://127.0.0.1:%d" % port


#: port → FakeCos（同进程内 e2e_server 要拿回同一个实例做拼接证据）
_REGISTRY: dict[int, "FakeCos"] = {}
_REGISTRY_LOCK = threading.Lock()


def fake_for_port(port) -> "FakeCos":
    """取回 ``start()`` 起的那个假桶（同进程内；拿不到即装配错误）。"""
    with _REGISTRY_LOCK:
        fake = _REGISTRY.get(int(port))
    if fake is None:
        raise RuntimeError("假 COS 未在本进程注册（port=%r）" % (port,))
    return fake


def install_cos_client(port, *, verify_ssl=False):
    """把 ``cos_client._client`` 指向假 COS（生产代码零改动，注入点在测试侧）。

    ``Domain=<host:port>`` 是唯一正确的旋钮：path-style 预签名 URL 形如
    ``http://127.0.0.1:<port>/incoming/...``（``Endpoint=`` 会产出
    ``<bucket>.127.0.0.1:<port>`` 这类坏主机，见 ``cos_client`` 模块注释与
    设计 §10.16）。``_client_cache`` 必须清空——否则会用旧配置构造的客户端。
    """
    from qcloud_cos import CosConfig, CosS3Client

    import cos_client

    def factory():
        return CosS3Client(CosConfig(
            Region=REGION, SecretId=SECRET_ID, SecretKey=SECRET_KEY,
            Scheme="http", Domain="127.0.0.1:%d" % port,
            VerifySSL=verify_ssl, Timeout=30))

    cos_client._client_cache.clear()
    cos_client._client = factory          # noqa: SLF001 — 测试注入点
    return factory
