#!/usr/bin/env python3
"""COS→server download-leg bench (audit-plan §7 Phase 0 三段吞吐, third leg).

The server connects DIRECTLY to the COS public endpoint
(<bucket>.cos.<region>.myqcloud.com) — never through frp — and GETs the given
key, recording bytes, duration, decimal Mbps and failure counts.

Credentials: `--auth sts` (default) first issues a GetObject-only single-key
STS via the operator env creds — mirroring the production worker's
least-privilege download credential — then signs the GET with the temporary
key. `--auth direct` signs with the long-term env creds (server-side only).

The other two legs (browser→COS, browser→platform/frp) are measured per the
written procedures in README §三段吞吐测量规程; results from all three legs use
the same evidence block format (tools/evidence_log.py) appended to
docs/evidence/cos-20260924.md.

No secrets in logs: only sizes, timings, status codes, request ids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
from pathlib import Path
from typing import List, Optional, Tuple

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT / "tools"))

import poc_config  # noqa: E402
from cos_xml_signing import signed_request  # noqa: E402
from evidence_log import append_block, throughput_lines  # noqa: E402

REPO_ROOT = POC_ROOT.parents[1]                      # .../PathTogether
DEFAULT_EVIDENCE = REPO_ROOT / "docs" / "evidence" / "cos-20260924.md"
READ_CHUNK = 1_000_000  # 1 MB decimal read granularity for timing bytes


def issue_read_sts(cfg: poc_config.PocConfig, full_key: str, duration_seconds: int = 900) -> dict:
    """GetObject-only single-key STS (worker download model, §3.0 条件 2 spirit:
    narrow, short-lived, no write/list/delete)."""
    cfg.require_credentials()
    from sts.sts import Sts

    appid = cfg.bucket.rsplit("-", 1)[1]
    policy = {
        "version": "2.0",
        "statement": [{
            "effect": "allow",
            "action": ["name/cos:GetObject"],
            "resource": [f"qcs::cos:{cfg.region}:uid/{appid}:{cfg.bucket}/{full_key}"],
        }],
    }
    sts = Sts({
        "secret_id": cfg.secret_id,
        "secret_key": cfg.secret_key,
        "duration_seconds": int(duration_seconds),
        "policy": policy,
    })
    raw = sts.get_credential()
    credentials = raw.get("credentials", {})
    return {
        "secret_id": credentials.get("tmpSecretId"),
        "secret_key": credentials.get("tmpSecretKey"),
        "token": credentials.get("sessionToken"),
        "expired_time": raw.get("expiredTime"),
    }


def timed_get(cfg: poc_config.PocConfig, key: str, cred: Optional[dict],
              expect_sha256: Optional[str]) -> Tuple[int, float, float, Optional[str], Optional[int], str]:
    """One GET attempt → (bytes, ttfb_seconds, total_seconds, sha256, status, error)."""
    started = time.perf_counter()
    try:
        resp = signed_request(
            scheme="https", host=cfg.cos_host, method="GET", key=key,
            secret_id=cred["secret_id"] if cred else cfg.secret_id,
            secret_key=cred["secret_key"] if cred else cfg.secret_key,
            security_token=cred["token"] if cred else None,
            expires_in=600, timeout=600,
        )
        status = resp.status
        sha = hashlib.sha256()
        total_bytes = 0
        first_chunk_at: Optional[float] = None
        while True:
            block = resp.read(READ_CHUNK)
            if not block:
                break
            if first_chunk_at is None:
                first_chunk_at = time.perf_counter()
            total_bytes += len(block)
            if expect_sha256:
                sha.update(block)
        finished = time.perf_counter()
        ttfb = (first_chunk_at or started) - started
        digest = sha.hexdigest() if expect_sha256 else None
        err = ""
        if expect_sha256 and digest != expect_sha256:
            err = "sha256_mismatch"
        return (total_bytes, ttfb, finished - started, digest, status, err)
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        return (0, 0.0, time.perf_counter() - started, None, exc.code, f"http_error:{exc.code}")
    except Exception as exc:
        return (0, 0.0, time.perf_counter() - started, None, None, type(exc).__name__)


def lookup_manifest_sha(manifest_path: Path, key: str) -> Optional[str]:
    """Cross-check the downloaded object against the generator's manifest."""
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    size_hint = None
    for token in key.replace(".", "-").split("-"):
        if token.isdigit() and len(token) >= 7:
            size_hint = int(token)
            break
    for entry in manifest.get("files", []):
        if size_hint is not None and entry.get("size_bytes") == size_hint:
            return entry.get("sha256")
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--key", required=True, help="object key under the poc/ prefix")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--auth", choices=["sts", "direct"], default="sts")
    parser.add_argument("--manifest", default=None,
                        help="make_test_files.py manifest.json for SHA-256 cross-check")
    parser.add_argument("--expect-sha256", default=None)
    parser.add_argument("--append-evidence", default=str(DEFAULT_EVIDENCE))
    parser.add_argument("--no-append", action="store_true", help="print only; do not touch the evidence file")
    args = parser.parse_args(argv)

    try:
        cfg = poc_config.load_config(require=True)
    except poc_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    key = cfg.full_key(args.key)

    expect_sha = args.expect_sha256
    if not expect_sha and args.manifest:
        expect_sha = lookup_manifest_sha(Path(args.manifest), key)
        if expect_sha:
            print(f"manifest cross-check enabled (sha256 prefix {expect_sha[:16]}…)")

    cred = None
    if args.auth == "sts":
        cred = issue_read_sts(cfg, key)
        print(f"STS read credential issued (expires epoch {cred['expired_time']}); values not shown")

    bytes_list: List[int] = []
    seconds_list: List[float] = []
    failures = 0
    for attempt in range(1, args.repeats + 1):
        total, ttfb, total_s, digest, status, err = timed_get(cfg, key, cred, expect_sha)
        if err or not total:
            failures += 1
            print(f"attempt {attempt}: FAIL bytes=0 status={status} err={err}")
            bytes_list.append(0)
            seconds_list.append(0.0)
            continue
        mbps = total * 8 / total_s / 1_000_000
        print(f"attempt {attempt}: bytes={total} ttfb={ttfb:.3f}s total={total_s:.3f}s "
              f"Mbps={mbps:.2f} status={status}"
              + (f" sha256={'match' if digest == expect_sha else 'MISMATCH'}" if expect_sha else ""))
        bytes_list.append(total)
        seconds_list.append(total_s)

    if not args.no_append:
        append_block(
            Path(args.append_evidence),
            "三段吞吐 · COS→服务器（直连 endpoint，不绕 frp）",
            throughput_lines(
                leg="cos-to-server-direct",
                key=key,
                bytes_per_attempt=bytes_list,
                seconds_per_attempt=seconds_list,
                failures=failures,
                notes=f"auth={args.auth}; endpoint={cfg.cos_host}; "
                      f"sha256_check={'on' if expect_sha else 'off'}; "
                      f"repeats={args.repeats}; read_chunk={READ_CHUNK}",
            ),
        )
        print(f"evidence appended: {args.append_evidence}")
    return 1 if failures == args.repeats else 0


if __name__ == "__main__":
    sys.exit(main())
