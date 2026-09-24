#!/usr/bin/env python3
"""PoC STS issuer for candidate B (official SDK + single-key STS).

Contract: docs/cos-direct-upload-audit-plan.md §3.2 and §3.0 判定条件 1/2.
The CAM policy template `policies/cam-single-key.json` locks the temporary
credential to ONE random object key under the poc/ prefix, write-actions only.

Discipline (audit contract §10 / §5):
- Secrets come ONLY from environment variables (see poc_config).
- Nothing here logs SecretId/SecretKey/session token; `--print-shape` prints
  a REDACTED shape so wiring can be verified without leaking values.
- `--dry-run` performs no network call: it only renders the filled policy
  (policy documents contain no secret material) and exits.

CLI examples:
  # no network; show the exact policy that would be sent to STS
  python3 tools/sts_issuer.py --dry-run --key poc/obj-0123-abcd... --duration 900

  # issue a real single-key STS (requires COS_POC_* env; will fail until the
  # operator provides credentials — that failure is recorded as
  # blocked_external_input, never worked around)
  python3 tools/sts_issuer.py --duration 900
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT / "tools"))

import poc_config  # noqa: E402

TEMPLATE_PATH = POC_ROOT / "policies" / "cam-single-key.json"

# JS SDK getAuthorization callback shape (cos-js-sdk-v5 v1.10.1 field names).
JS_SDK_FIELDS = ("TmpSecretId", "TmpSecretKey", "XCosSecurityToken",
                 "SecurityToken", "StartTime", "ExpiredTime")


def strip_jsonc(text: str) -> str:
    """Remove full-line // comments from the JSONC template (values in this
    template never contain '//' on a comment line, so line-based stripping is
    safe and conservative)."""
    kept: List[str] = []
    for line in text.splitlines():
        if re.match(r"^\s*//", line):
            continue
        kept.append(line)
    return "\n".join(kept)


def load_policy_template() -> dict:
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    return json.loads(strip_jsonc(raw))


def render_policy(cfg: poc_config.PocConfig, full_object_key: str) -> dict:
    """Fill the template placeholders. No secret values are involved."""
    appid = cfg.bucket.rsplit("-", 1)[1]
    template = json.dumps(load_policy_template())
    for placeholder, value in (
        ("{{REGION}}", cfg.region),
        ("{{APPID}}", appid),
        ("{{BUCKET}}", cfg.bucket),
        ("{{KEY}}", full_object_key),
    ):
        template = template.replace(placeholder, value)
    rendered = json.loads(template)
    for stmt in rendered.get("statement", []):
        for resource in stmt.get("resource", []):
            if "*" in resource.split("/", 3)[-1] or resource.endswith(":*"):
                raise RuntimeError(f"refusing to issue: wildcard resource {resource!r}")
    return rendered


def issue_sts(
    cfg: poc_config.PocConfig,
    full_object_key: str,
    duration_seconds: int = 900,
) -> Dict[str, object]:
    """Issue a single-key STS credential and return the JS-SDK-shaped dict.

    qcloud-python-sts 3.1.6's get_credential() fails on Python 3: it returns the
    HMAC signature as bytes and omits Region unless the caller passed one.
    This call uses the same legacy STS signature the SDK intends, with both
    fixes, and never logs the temporary credential.
    """
    cfg.require_credentials()
    if not cfg.region:
        raise poc_config.ConfigError("blocked_external_input: missing COS_POC_REGION")
    policy = render_policy(cfg, full_object_key)
    raw = _get_federation_token(cfg, policy, int(duration_seconds))
    credentials = raw.get("Credentials") or {}
    expired = raw.get("ExpiredTime")
    start = expired - int(duration_seconds) if isinstance(expired, int) else None
    token = credentials.get("Token")
    return {
        "TmpSecretId": credentials.get("TmpSecretId"),
        "TmpSecretKey": credentials.get("TmpSecretKey"),
        "XCosSecurityToken": token,
        "SecurityToken": token,
        "StartTime": start,
        "ExpiredTime": expired,
        "policy": policy,
        "request_id": raw.get("RequestId"),
    }


def _get_federation_token(
    cfg: poc_config.PocConfig,
    policy: dict,
    duration_seconds: int,
) -> Dict[str, object]:
    """POST GetFederationToken. Returns the Response object or raises."""
    import hashlib
    import hmac
    import base64
    import json as _json
    import random
    import time
    import urllib.parse
    import urllib.request
    from urllib.error import HTTPError

    data = {
        "SecretId": cfg.secret_id,
        "Timestamp": int(time.time()),
        "Nonce": random.randint(100000, 200000),
        "Action": "GetFederationToken",
        "Version": "2018-08-13",
        "DurationSeconds": int(duration_seconds),
        "Name": "cos-sts-poc",
        "Policy": urllib.parse.quote(_json.dumps(policy, separators=(",", ":"))),
        "Region": cfg.region,
    }
    source = "POSTsts.tencentcloudapi.com/?" + "&".join(
        f"{key}={data[key]}" for key in sorted(data)
    )
    digest = hmac.new(
        cfg.secret_key.encode("utf-8"), source.encode("utf-8"), hashlib.sha1
    ).digest()
    data["Signature"] = base64.b64encode(digest).decode("ascii")
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        "https://sts.tencentcloudapi.com/", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"sts http {exc.code}: {detail}") from exc
    response = payload.get("Response") or {}
    error = response.get("Error")
    if error:
        code = error.get("Code") or "unknown"
        message = (error.get("Message") or "")[:180]
        raise RuntimeError(f"sts {code}: {message}")
    return response


def redacted_shape(credential: Dict[str, object]) -> Dict[str, object]:
    """Log-safe view: only lengths/presence, never values."""
    out: Dict[str, object] = {}
    for field in ("TmpSecretId", "TmpSecretKey", "XCosSecurityToken"):
        value = credential.get(field)
        out[field + "_len"] = len(value) if isinstance(value, str) else None
    out["StartTime"] = credential.get("StartTime")
    out["ExpiredTime"] = credential.get("ExpiredTime")
    out["request_id"] = credential.get("request_id")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", default=None,
                        help="full object key under the poc/ prefix; default: newly generated random key")
    parser.add_argument("--duration", type=int, default=900,
                        help="STS TTL seconds (default 900; keep short — the §3.0 条件 3 window scales with this)")
    parser.add_argument("--dry-run", action="store_true",
                        help="render the filled policy, no network call")
    parser.add_argument("--print-shape", action="store_true",
                        help="after issuing, print a REDACTED credential shape (no secret values)")
    args = parser.parse_args(argv)

    try:
        cfg = poc_config.load_config(require=False)
        key = poc_config.random_key(cfg.prefix) if args.key is None else cfg.full_key(args.key)
    except poc_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            cfg.require_credentials()
        except poc_config.ConfigError as exc:
            # Policy rendering needs bucket/region only; still report what's absent.
            print(f"note: {exc}", file=sys.stderr)
            if not (cfg.bucket and cfg.region):
                return 2
        print(json.dumps(render_policy(cfg, key), indent=2, ensure_ascii=False))
        print(f"key: {key}")
        print(f"duration_seconds: {args.duration}")
        return 0

    try:
        credential = issue_sts(cfg, key, args.duration)
    except poc_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"issued STS for key {key} (ttl {args.duration}s)")
    if args.print_shape:
        print(json.dumps(redacted_shape(credential), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
