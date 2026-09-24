"""COS XML API request signing (Python port of the official algorithm).

Algorithm reference (official): https://cloud.tencent.com/document/product/436/7778
and the reference implementation shipped with cos-js-sdk-v5's demo server
(server/sts.js → `put-sign`), mirrored here step by step.

Used by:
- tools/presign_parts.py  (candidate A: UploadPart presigned URLs)
- tools/serve_poc.py      (dev-only admin audit/cleanup endpoints)
- tools/cos_download_bench.py (server-side GET)

Secrets are accepted as arguments and never logged. Signed URLs produced here
must not be written to files or logs (audit contract §10 redaction rules).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
import urllib.request
from typing import Dict, Mapping, Optional, Tuple


def cam_safe_url_encode(value: str) -> str:
    """URL-encode exactly like the official SDKs ('cam' safe encoding)."""
    # safe="" so '/' becomes %2F. encodeURIComponent encodes slashes; the default
    # quote() safe set keeps them, which breaks prefix/query signatures.
    return (
        urllib.parse.quote(str(value), safe="", encoding="utf-8")
        .replace("!", "%21")
        .replace("'", "%27")
        .replace("(", "%28")
        .replace(")", "%29")
        .replace("*", "%2A")
    )


def _obj2str(params: Mapping[str, object]) -> str:
    """k=v pairs, keys/values cam-encoded, sorted by lowercase encoded key."""
    items = sorted(
        ((cam_safe_url_encode(k).lower(), cam_safe_url_encode("" if v is None else str(v)))
         for k, v in params.items()),
        key=lambda kv: kv[0],
    )
    return "&".join(f"{k}={v}" for k, v in items)


def _key_list(params: Mapping[str, object]) -> str:
    return ";".join(sorted((cam_safe_url_encode(k).lower() for k in params)))


def object_path(key: str) -> str:
    """Path used both for signing and for the actual request ('/' + key).

    The signed string and the transmitted URL must be byte-identical, so both
    go through this function. Keep PoC keys ASCII (poc_config enforces this).
    """
    if not key:
        return "/"
    return "/" + "/".join(cam_safe_url_encode(seg) for seg in key.split("/"))


def sign_params(
    method: str,
    key: str,
    query: Optional[Mapping[str, object]] = None,
    headers: Optional[Mapping[str, object]] = None,
    secret_id: str = "",
    secret_key: str = "",
    expires_in: int = 600,
    now: Optional[int] = None,
) -> Dict[str, str]:
    """Return the q-* query parameters of a COS XML API signature.

    `query` values may be None to model bare parameters like `uploads`
    (serialized as an empty value, exactly like the JS SDK does).
    `headers` here are ONLY the headers bound into the signature
    (q-header-list). Everything not listed is not covered by the signature.
    """
    # COS 签名串里的 method 必须小写（官方算法文档 HTTPMethod）；线上请求仍发大写。
    method = method.lower()
    query = dict(query or {})
    headers = dict(headers or {})
    now = int(time.time()) if now is None else now
    t0 = now - 60  # 60s clock-skew allowance, mirrors official demo (now-1)
    t1 = now + int(expires_in)
    key_time = f"{t0};{t1}"

    sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()

    format_string = "\n".join(
        [method, object_path(key), _obj2str(query), _obj2str(headers), ""]
    )
    sha1ed = hashlib.sha1(format_string.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join(["sha1", key_time, sha1ed, ""])
    signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()

    return {
        "q-sign-algorithm": "sha1",
        "q-ak": secret_id,
        "q-sign-time": key_time,
        "q-key-time": key_time,
        "q-header-list": _key_list(headers),
        "q-url-param-list": _key_list(query),
        "q-signature": signature,
    }


def presign_url(
    scheme: str,
    host: str,
    method: str,
    key: str,
    query: Optional[Mapping[str, object]] = None,
    bound_headers: Optional[Mapping[str, object]] = None,
    secret_id: str = "",
    secret_key: str = "",
    expires_in: int = 600,
    security_token: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """Build a presigned URL plus the exact header set the caller must send.

    If security_token is given (STS temporary credentials) it is added as the
    x-cos-security-token query parameter and covered by the signature.
    If bound_headers contains e.g. content-length, the caller MUST send the
    byte-identical header value or COS rejects with a signature error — this
    is how candidate A binds Content-Length where feasible (合同 §3.1).
    """
    q = dict(query or {})
    if security_token:
        q = {**q, "x-cos-security-token": security_token}
    params = sign_params(
        method=method,
        key=key,
        query=q,
        headers=bound_headers,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_in=expires_in,
    )
    encoded = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items())
    extra = ""
    if q:
        extra = "?" + "&".join(
            f"{cam_safe_url_encode(k)}={cam_safe_url_encode('' if v is None else str(v))}"
            for k, v in sorted(q.items(), key=lambda kv: kv[0].lower())
        )
    url = f"{scheme}://{host}{object_path(key)}{extra}{extra and '&' or '?'}{encoded}"
    return url, dict(bound_headers or {})


def signed_request(
    scheme: str,
    host: str,
    method: str,
    key: str,
    query: Optional[Mapping[str, object]] = None,
    headers: Optional[Mapping[str, object]] = None,
    body: Optional[bytes] = None,
    secret_id: str = "",
    secret_key: str = "",
    expires_in: int = 600,
    security_token: Optional[str] = None,
    timeout: int = 60,
) -> urllib.request.Response:
    """Issue a server-side signed COS XML API call with long-term or STS creds.

    Header signing rule (same as official SDKs): `host` is always signed; any
    header we promise in q-header-list must be sent unchanged.
    """
    q = dict(query or {})
    if security_token:
        q = {**q, "x-cos-security-token": security_token}
    send_headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    send_headers.setdefault("host", host)
    signed = sign_params(
        method=method,
        key=key,
        query=q,
        headers=send_headers,
        secret_id=secret_id,
        secret_key=secret_key,
        expires_in=expires_in,
    )
    encoded = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in signed.items())
    extra = ""
    if q:
        extra = "?" + "&".join(
            f"{cam_safe_url_encode(k)}={cam_safe_url_encode('' if v is None else str(v))}"
            for k, v in sorted(q.items(), key=lambda kv: kv[0].lower())
        )
    url = f"{scheme}://{host}{object_path(key)}{extra}{extra and '&' or '?'}{encoded}"
    req = urllib.request.Request(url, data=body, method=method.upper())
    for k, v in send_headers.items():
        if k != "host":  # host header is set by urllib from the URL
            req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def redact_url(url: str) -> str:
    """Mask signature material for logging: keep host/path/param names only."""
    parsed = urllib.parse.urlsplit(url)
    kept = []
    for part in parsed.query.split("&"):
        name = part.split("=", 1)[0]
        if name in ("q-ak", "q-signature", "q-sign-time", "q-key-time", "x-cos-security-token"):
            kept.append(f"{name}=<redacted>")
        else:
            kept.append(f"{name}=<value>")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "&".join(kept), "")
    )
