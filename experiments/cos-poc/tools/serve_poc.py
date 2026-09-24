#!/usr/bin/env python3
"""Local dev server for the candidate-B PoC page: static assets + STS endpoint
+ dev-only admin audit/cleanup endpoints. Python stdlib only.

Scope discipline (audit contract §10 / §5):
- DEV ONLY. Binds 127.0.0.1 by default. The production-origin CORS/CSP
  validation runs through the approved release path in a low-traffic window —
  it is NOT this server's job.
- Secrets only from environment (poc_config). Nothing logs credential values
  or signed URLs; request logs are method+path only (query stripped).
- /api/poc-sts responds 503 with `blocked_external_input` + the missing env
  names when PoC credentials are absent — no partial, no mocks.

Admin endpoints (audit/cleanup) use the OPERATOR's long-term PoC credentials
server-side, mirroring where the production worker's broader credentials would
live; the browser STS itself never gains read/delete/list rights (§3.0 条件 2).
"""

from __future__ import annotations

import argparse
import hmac
import json
import sys
import urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT / "tools"))

import poc_config  # noqa: E402
import sts_issuer  # noqa: E402
from cos_xml_signing import signed_request  # noqa: E402

STATIC_FILES = {
    "/": (POC_ROOT / "web" / "poc_b.html", "text/html; charset=utf-8"),
    "/poc_b.js": (POC_ROOT / "web" / "poc_b.js", "application/javascript; charset=utf-8"),
    "/poc_b.html": (POC_ROOT / "web" / "poc_b.html", "text/html; charset=utf-8"),
    "/vendor/cos-js-sdk-v5.min.js": (
        POC_ROOT / "vendor" / "cos-js-sdk-v5-1.10.1.min.js",
        "application/javascript; charset=utf-8",
    ),
}

DEFAULT_STS_TTL = 900


# ---------------------------------------------------------------- COS helpers
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> List[ET.Element]:
    return [el for el in root.iter() if _local(el.tag) == name]


def _find_text(root: ET.Element, name: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == name:
            return el.text
    return None


def _cos_xml(cfg: poc_config.PocConfig, method: str, key: str, query: Dict[str, object]):
    """Signed call returning parsed XML root; HTTPError propagated to caller."""
    resp = signed_request(
        scheme="https",
        host=cfg.cos_host,
        method=method,
        key=key,
        query=query,
        secret_id=cfg.secret_id,
        secret_key=cfg.secret_key,
        expires_in=300,
        timeout=60,
    )
    body = resp.read()
    return ET.fromstring(body)


def list_object_versions(cfg: poc_config.PocConfig, key: str) -> Tuple[List[dict], bool]:
    """All versions + delete markers under exactly this key (prefix=exact key)."""
    versions: List[dict] = []
    key_marker = ""
    version_marker = ""
    truncated = False
    while True:
        query: Dict[str, object] = {"versions": "", "prefix": key, "max-keys": "1000"}
        if key_marker:
            query["key-marker"] = key_marker
        if version_marker:
            query["version-id-marker"] = version_marker
        root = _cos_xml(cfg, "GET", "", query)
        for entry in root.iter():
            local = _local(entry.tag)
            if local not in ("Version", "DeleteMarker"):
                continue
            record = {
                "kind": "delete_marker" if local == "DeleteMarker" else "version",
                "key": _find_text(entry, "Key"),
                "version_id": _find_text(entry, "VersionId"),
                "is_latest": _find_text(entry, "IsLatest"),
                "last_modified": _find_text(entry, "LastModified"),
                "size_bytes": int(_find_text(entry, "Size") or 0),
            }
            if record["key"] == key:
                versions.append(record)
        truncated = (_find_text(root, "IsTruncated") or "").lower() == "true"
        if not truncated:
            break
        next_key = _find_text(root, "NextKeyMarker")
        next_version = _find_text(root, "NextVersionIdMarker")
        if not next_key and not next_version:
            break
        if next_key == key_marker and next_version == version_marker:
            break  # defensive: never loop forever on a bad marker
        key_marker, version_marker = next_key or key_marker, next_version or version_marker
    return versions, truncated


def list_multipart_uploads(cfg: poc_config.PocConfig, prefix: str) -> List[dict]:
    uploads: List[dict] = []
    key_marker = ""
    upload_marker = ""
    while True:
        query: Dict[str, object] = {"uploads": "", "prefix": prefix, "max-uploads": "1000"}
        if key_marker:
            query["key-marker"] = key_marker
        if upload_marker:
            query["upload-id-marker"] = upload_marker
        root = _cos_xml(cfg, "GET", "", query)
        for entry in _find_all(root, "Upload"):
            record = {
                "key": _find_text(entry, "Key"),
                "upload_id": _find_text(entry, "UploadId"),
                "initiated": _find_text(entry, "Initiated"),
                "storage_class": _find_text(entry, "StorageClass"),
            }
            if record["key"] == prefix or prefix == "":
                uploads.append(record)
        if (_find_text(root, "IsTruncated") or "").lower() != "true":
            break
        next_key = _find_text(root, "NextKeyMarker") or key_marker
        next_upload = _find_text(root, "NextUploadIdMarker") or upload_marker
        if next_key == key_marker and next_upload == upload_marker:
            break
        key_marker, upload_marker = next_key, next_upload
    return uploads


def list_parts(cfg: poc_config.PocConfig, key: str, upload_id: str) -> List[dict]:
    parts: List[dict] = []
    marker = 0
    while True:
        query: Dict[str, object] = {"uploadId": upload_id, "max-parts": "1000"}
        if marker:
            query["part-number-marker"] = str(marker)
        root = _cos_xml(cfg, "GET", key, query)
        for entry in _find_all(root, "Part"):
            parts.append({
                "part_number": int(_find_text(entry, "PartNumber") or 0),
                "size_bytes": int(_find_text(entry, "Size") or 0),
                "etag": _find_text(entry, "ETag"),
            })
        if (_find_text(root, "IsTruncated") or "").lower() != "true":
            break
        next_marker = int(_find_text(root, "NextPartNumberMarker") or 0)
        if next_marker <= marker:
            break
        marker = next_marker
    return parts


def abort_upload(cfg: poc_config.PocConfig, key: str, upload_id: str) -> int:
    resp = signed_request(
        scheme="https", host=cfg.cos_host, method="DELETE", key=key,
        query={"uploadId": upload_id},
        secret_id=cfg.secret_id, secret_key=cfg.secret_key, expires_in=300, timeout=60,
    )
    resp.read()
    return resp.status


def delete_version(cfg: poc_config.PocConfig, key: str, version_id: str) -> int:
    resp = signed_request(
        scheme="https", host=cfg.cos_host, method="DELETE", key=key,
        query={"VersionId": version_id},
        secret_id=cfg.secret_id, secret_key=cfg.secret_key, expires_in=300, timeout=60,
    )
    resp.read()
    return resp.status


# ---------------------------------------------------------------- audit logic
def audit_key_occupancy(cfg: poc_config.PocConfig, key: str) -> dict:
    """§3.0 条件 3 evidence: bytes of ALL versions + in-flight multipart parts
    of one key at the moment of the call. Repeat during the credential TTL —
    post-hoc cleanup does NOT satisfy the hard gate."""
    versions, list_truncated = list_object_versions(cfg, key)
    uploads = list_multipart_uploads(cfg, key)
    uploads_detail = []
    parts_total = 0
    for upload in uploads:
        try:
            parts = list_parts(cfg, key, upload["upload_id"])
        except urllib.error.HTTPError as exc:  # completed-and-listed uploads vanish
            uploads_detail.append({**upload, "parts": [], "error": f"http {exc.code}"})
            continue
        parts_total += sum(p["size_bytes"] for p in parts)
        uploads_detail.append({**upload, "parts": parts})

    version_bytes = sum(v["size_bytes"] for v in versions if v["kind"] == "version")
    delete_markers = sum(1 for v in versions if v["kind"] == "delete_marker")
    return {
        "key": key,
        "versions": versions,
        "multipart_uploads": uploads_detail,
        "totals": {
            "object_versions_bytes": version_bytes,
            "version_count": len(versions),
            "delete_marker_count": delete_markers,
            "multipart_parts_bytes": parts_total,
            "multipart_upload_count": len(uploads_detail),
            "list_truncated": list_truncated,
            "grand_total_bytes": version_bytes + parts_total,
        },
        "note": "compare grand_total_bytes against the task's reserved_bytes "
                "(= declared size); breach at ANY point inside the STS TTL fails "
                "audit-plan §3.0 condition 3 even if later cleaned",
    }


def cleanup_key(cfg: poc_config.PocConfig, key: str) -> dict:
    """Abort all in-flight multiparts of the key, then delete every version and
    delete marker by exact versionId (§3.2 全版本清理 — 删一次 key 不算清理完成)."""
    steps: List[dict] = []
    for upload in list_multipart_uploads(cfg, key):
        try:
            code = abort_upload(cfg, key, upload["upload_id"])
            steps.append({"step": "abort", "upload_id": upload["upload_id"], "http": code})
        except urllib.error.HTTPError as exc:
            steps.append({"step": "abort", "upload_id": upload["upload_id"], "http": exc.code, "error": str(exc.reason)})
    removed = 0
    failures = 0
    for _ in range(5):  # deleting versions can itself create new delete markers; loop until stable
        versions, _ = list_object_versions(cfg, key)
        if not versions:
            break
        for version in versions:
            try:
                delete_version(cfg, key, version["version_id"])
                removed += 1
            except urllib.error.HTTPError as exc:
                failures += 1
                steps.append({"step": "delete_version", "version_id": version["version_id"], "http": exc.code})
    remaining, _ = list_object_versions(cfg, key)
    steps.append({"step": "verify_remaining", "remaining_version_count": len(remaining)})
    return {"key": key, "steps": steps, "versions_removed": removed, "delete_failures": failures,
            "cleaned": not remaining}


# ---------------------------------------------------------------- HTTP server
class PocHandler(BaseHTTPRequestHandler):
    server_version = "cos-poc-dev/1.0"
    cfg: poc_config.PocConfig = None  # injected

    def log_message(self, fmt, *args):  # redact: method + path only, never query
        try:
            path = self.path.split("?", 1)[0]
            sys.stderr.write("[poc-dev] %s %s\n" % (self.command, path))
        except Exception:
            pass

    # -- plumbing ---------------------------------------------------------
    def _send_json(self, status: int, payload: dict, extra_headers: Optional[dict] = None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _check_admin_token(self) -> bool:
        expected = self.cfg.admin_token
        if not expected:
            return True  # localhost-bind is the guard when no token configured
        supplied = self.headers.get("X-PoC-Admin-Token", "")
        return hmac.compare_digest(expected, supplied)

    # -- routes -------------------------------------------------------------
    def do_OPTIONS(self):  # same-origin page; still answer preflight cleanly
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in STATIC_FILES:
            file_path, mime = STATIC_FILES[path]
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if mime.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; "
                    "connect-src 'self' https://*.cos." + (self.cfg.region or "*") + ".myqcloud.com; "
                    "img-src 'self' data:",
                )
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/poc-sts":
            self._handle_sts()
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def _handle_sts(self):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        requested_key = (query.get("key") or [""])[0]
        cfg = self.cfg
        if not cfg.has_credentials or not cfg.bucket or not cfg.region:
            self._send_json(503, {
                "ok": False,
                "error": "blocked_external_input",
                "missing": cfg.missing,
                "note": "set COS_POC_SECRET_ID/SECRET_KEY/BUCKET/REGION; "
                        "do not fabricate or mock credentials (audit-plan §10)",
            })
            return
        try:
            key = cfg.full_key(requested_key) if requested_key else poc_config.random_key(cfg.prefix)
            ttl = getattr(self.server, "sts_ttl", DEFAULT_STS_TTL)
            credential = sts_issuer.issue_sts(cfg, key, ttl)
        except poc_config.ConfigError as exc:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": str(exc)[:300]})
            return
        except Exception as exc:  # STS backend failure — surface code, never secrets
            self._send_json(502, {"ok": False, "error": "sts_error", "detail": type(exc).__name__})
            return
        self._send_json(200, {
            "ok": True,
            "bucket": cfg.bucket,
            "region": cfg.region,
            "key": key,
            "startTime": credential["StartTime"],
            "expiredTime": credential["ExpiredTime"],
            # credentials live in this response body only; never logged:
            "credentials": {
                "tmpSecretId": credential["TmpSecretId"],
                "tmpSecretKey": credential["TmpSecretKey"],
                "sessionToken": credential["XCosSecurityToken"],
            },
        })

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/poc-admin/audit", "/api/poc-admin/cleanup"):
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        if not self._check_admin_token():
            self._send_json(403, {"ok": False, "error": "bad_admin_token"})
            return
        cfg = self.cfg
        if not cfg.has_credentials or not cfg.bucket or not cfg.region:
            self._send_json(503, {"ok": False, "error": "blocked_external_input", "missing": cfg.missing})
            return
        body = self._read_json_body()
        raw_key = str(body.get("key") or "")
        try:
            key = cfg.full_key(raw_key)
        except poc_config.ConfigError as exc:
            self._send_json(400, {"ok": False, "error": "bad_request", "detail": str(exc)[:300]})
            return
        try:
            if path == "/api/poc-admin/audit":
                result = audit_key_occupancy(cfg, key)
            else:
                result = cleanup_key(cfg, key)
        except urllib.error.HTTPError as exc:
            self._send_json(502, {"ok": False, "error": "cos_http_error", "status": exc.code})
            return
        except Exception as exc:
            self._send_json(500, {"ok": False, "error": type(exc).__name__})
            return
        self._send_json(200, {"ok": True, **result})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1; dev only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sts-ttl", type=int, default=DEFAULT_STS_TTL,
                        help="STS TTL seconds served to the page (default %d)" % DEFAULT_STS_TTL)
    args = parser.parse_args(argv)

    cfg = poc_config.load_config(require=False)
    PocHandler.cfg = cfg
    httpd = ThreadingHTTPServer((args.host, args.port), PocHandler)
    httpd.sts_ttl = args.sts_ttl
    print(f"[poc-dev] serving http://{args.host}:{args.port}/  (config: {cfg.describe()})", file=sys.stderr)
    print("[poc-dev] NOTE: /api/poc-sts returns 503 blocked_external_input until "
          "COS_POC_* credentials/bucket/region are provided", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
