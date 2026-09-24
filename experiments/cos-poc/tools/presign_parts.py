#!/usr/bin/env python3
"""Candidate A PoC toolchain: platform-controlled multipart + per-part
presigned UploadPart URLs (audit-plan §3.1, §3.0 A-conditions).

Shared configuration with candidate B: same COS_POC_* environment variables
(tools/poc_config.py). No browser credentials exist in model A — the platform
signs each part; the browser only PUTs bytes to the signed URL.

Subcommands:
  plan     — print the decimal part plan for a declared size (worker-side
             authority; the browser's self-declared length is never trusted).
  sign     — build ONE UploadPart presigned URL bound to
             method/host/key/uploadId/partNumber (+ Content-Length when
             possible). URLs are never written to files; --stdout prints the
             URL transiently for piping into a test driver, and the tool
             warns against redirecting it anywhere.
  selftest — end-to-end negative suite against real COS (requires operator
             env credentials; refuses politely with blocked_external_input
             otherwise):
               1. control: exact-size part PUT → 200
               2. oversized part (declared N, sends N+1) → must be rejected
               3. truncated part (declared N, sends N-k) → must be rejected
               4. out-of-plan / tampered partNumber in URL → 403
               5. expired URL replay → 403
               6. presigned query reused for Initiate/Complete/GET/DELETE → 403
               7. unbound variant (--no-bind-content-length) recorded as a
                  finding when oversized parts are ACCEPTED (plan §3.1: cannot
                  claim a hard cloud capacity limit without a binding).

Decimal byte discipline (§0 D3): part sizes are decimal integers; default
part size is 32000000 (32 MB decimal). The contract's "32 MiB PoC 起点"
(§3.1) equals 33554432 and can be passed explicitly via --part-bytes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT / "tools"))

import poc_config  # noqa: E402
from cos_xml_signing import presign_url, redact_url, signed_request  # noqa: E402

DEFAULT_PART_BYTES = 32_000_000  # 32 MB decimal (contract §3.1 says 32 MiB == 33554432; pass explicitly if wanted)
PROBE_PART_BYTES = 1_000_000    # small decimal probe so selftest costs nothing
REPO_ROOT = POC_ROOT.parents[1]                      # .../PathTogether
DEFAULT_EVIDENCE = REPO_ROOT / "docs" / "evidence" / "cos-20260924.md"


def compute_plan(size_bytes: int, part_bytes: int) -> List[dict]:
    if size_bytes <= 0:
        raise SystemExit("size must be a positive decimal integer")
    if part_bytes <= 0:
        raise SystemExit("part size must be a positive decimal integer")
    plan = []
    offset = 0
    number = 1
    while offset < size_bytes:
        length = min(part_bytes, size_bytes - offset)
        plan.append({"part_number": number, "offset": offset, "length": length})
        offset += length
        number += 1
    return plan


# --------------------------------------------------------------- remote steps
def initiate_multipart(cfg, key: str) -> str:
    resp = signed_request(
        scheme="https", host=cfg.cos_host, method="POST", key=key,
        query={"uploads": ""},
        secret_id=cfg.secret_id, secret_key=cfg.secret_key, expires_in=300, timeout=60,
    )
    root = ET.fromstring(resp.read())
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "UploadId":
            return el.text
    raise RuntimeError("no UploadId in InitiateMultipartUpload response")


def abort_multipart(cfg, key: str, upload_id: str) -> int:
    resp = signed_request(
        scheme="https", host=cfg.cos_host, method="DELETE", key=key,
        query={"uploadId": upload_id},
        secret_id=cfg.secret_id, secret_key=cfg.secret_key, expires_in=300, timeout=60,
    )
    resp.read()
    return resp.status


def put_presigned(url: str, data: bytes, bound_headers: Optional[dict]) -> Tuple[str, Optional[int], str]:
    """PUT bytes at a presigned URL. Returns (outcome, status, detail).

    Content-Length is deliberately NOT added manually: urllib derives it from
    the actual body, exactly like a browser XHR/fetch would. When the URL is
    signed with a bound content-length, any mismatch (oversized/truncated
    body) therefore surfaces as a COS signature error — the exact behavior
    candidate A depends on. Manually forcing the signed header would instead
    desynchronize header vs body and turn the test into a transport artifact.
    """
    req = urllib.request.Request(url, data=data, method="PUT")
    for k, v in (bound_headers or {}).items():
        if k.lower() in ("host", "content-length"):
            continue  # host comes from the URL; content-length from the body
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
            return ("ok", resp.status, "")
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        return ("http_error", exc.code, str(exc.reason or "")[:80])
    except Exception as exc:  # connection reset etc.
        return ("transport_error", None, type(exc).__name__)


def build_part_url(cfg, key: str, upload_id: str, part_number: int, content_length: Optional[int],
                   bind: bool, expires_in: int) -> Tuple[str, dict]:
    query = {"partNumber": str(part_number), "uploadId": upload_id}
    headers = {}
    if bind and content_length is not None:
        headers["content-length"] = str(content_length)
    return presign_url(
        scheme="https", host=cfg.cos_host, method="PUT", key=key, query=query,
        bound_headers=headers,
        secret_id=cfg.secret_id, secret_key=cfg.secret_key, expires_in=expires_in,
    )


# ------------------------------------------------------------------ commands
def cmd_plan(args) -> int:
    plan = compute_plan(args.size, args.part_bytes)
    for p in plan:
        print(f"part {p['part_number']:>5}  offset {p['offset']:>12}  length {p['length']:>12}")
    total = sum(p["length"] for p in plan)
    print(f"parts={len(plan)} total={total} declared={args.size} match={total == args.size}")
    return 0


def cmd_sign(args) -> int:
    try:
        cfg = poc_config.load_config(require=True)
    except poc_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    url, headers = build_part_url(cfg, args.key, args.upload_id, args.part_number,
                                  args.content_length, bind=not args.no_bind,
                                  expires_in=args.expires)
    if args.stdout:
        print("WARNING: presigned URL carries signing authority. Do not redirect, tee, "
              "paste into tickets, or commit it. It expires in %ds." % args.expires,
              file=sys.stderr)
        print(url)
        if headers:
            print("must-send-headers: " + json.dumps(headers), file=sys.stderr)
    else:
        print("presigned URL built (redacted view):")
        print("  " + redact_url(url))
        print(f"  bound headers: {headers or 'none'}")
        print("  re-run with --stdout to emit the working URL to the terminal only")
    return 0


def cmd_selftest(args) -> int:
    try:
        cfg = poc_config.load_config(require=True)
    except poc_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    import os
    key = poc_config.random_key(cfg.prefix, label="a-parts")
    print(f"key: {key}")
    upload_id = initiate_multipart(cfg, key)
    print(f"upload_id: {upload_id} (initiated by operator creds, worker-style)")

    results: List[dict] = []

    def run_case(name: str, expectation: str, fn) -> None:
        outcome, status, detail = fn()
        passed = False
        if expectation == "reject" and outcome == "http_error" and status in (400, 403, 413):
            passed = True
        if expectation == "accept" and outcome == "ok":
            passed = True
        results.append({"case": name, "expected": expectation, "outcome": outcome,
                        "status": status, "detail": detail, "pass": passed})
        print(f"  {'PASS' if passed else 'FAIL'}  {name}: expected {expectation}, "
              f"got {outcome} status={status} {detail}")

    probe = lambda n: os.urandom(n)

    for bind in ([True, False] if args.test_unbound else [True]):
        mode = "bound" if bind else "UNBOUND"
        url, headers = build_part_url(cfg, key, upload_id, 1, PROBE_PART_BYTES, bind=bind, expires_in=600)
        run_case(
            f"[{mode}] control exact-size part",
            "accept",
            lambda: put_presigned(url, probe(PROBE_PART_BYTES), headers),
        )
        url2, headers2 = build_part_url(cfg, key, upload_id, 2, PROBE_PART_BYTES, bind=bind, expires_in=600)
        run_case(
            f"[{mode}] oversized part (declared {PROBE_PART_BYTES}, sends {PROBE_PART_BYTES + 1})",
            "reject",
            lambda: put_presigned(url2, probe(PROBE_PART_BYTES + 1), headers2),
        )
        url3, headers3 = build_part_url(cfg, key, upload_id, 3, PROBE_PART_BYTES, bind=bind, expires_in=600)
        run_case(
            f"[{mode}] truncated part (declared {PROBE_PART_BYTES}, sends {PROBE_PART_BYTES - 1000})",
            "reject",
            lambda: put_presigned(url3, probe(PROBE_PART_BYTES - 1000), headers3),
        )

    # tampered partNumber (signature must not verify)
    url4, _ = build_part_url(cfg, key, upload_id, 4, PROBE_PART_BYTES, bind=True, expires_in=600)
    tampered = url4.replace("partNumber=4", "partNumber=404")
    if tampered == url4:
        tampered = url4.replace("partNumber=4&", "partNumber=404&")
    run_case("tampered partNumber in URL", "reject", lambda: put_presigned(tampered, probe(PROBE_PART_BYTES), {}))

    # method misuse: presigned query is method-bound; anything but PUT must 403
    for method in ("GET", "DELETE", "POST"):
        req = urllib.request.Request(url4, method=method,
                                     data=b"x" if method == "POST" else None)
        def attempt(req=req):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
                    return ("ok", resp.status, "")
            except urllib.error.HTTPError as exc:
                exc.read()
                return ("http_error", exc.code, str(exc.reason)[:60])
            except Exception as exc:
                return ("transport_error", None, type(exc).__name__)
        run_case(f"presigned query reused as {method} (Initiate/GetObject/Delete stand-in)", "reject", attempt)

    if not args.fast:
        print("  … expiry replay: waiting ~70s for a 1s-TTL URL to lapse (use --fast to skip)")
        url5, headers5 = build_part_url(cfg, key, upload_id, 5, PROBE_PART_BYTES, bind=True, expires_in=1)
        time.sleep(70)
        run_case("expired URL replay", "reject", lambda: put_presigned(url5, probe(PROBE_PART_BYTES), headers5))
    else:
        results.append({"case": "expired URL replay", "expected": "reject",
                        "outcome": "skipped", "status": None, "detail": "--fast", "pass": None})
        print("  SKIP  expired URL replay (--fast)")

    # cleanup
    try:
        abort_multipart(cfg, key, upload_id)
        print("cleanup: multipart aborted")
    except Exception as exc:
        print(f"cleanup FAILED ({type(exc).__name__}) — record it; leftovers count against pool bytes")

    failures = [r for r in results if r["pass"] is False]
    print(f"\nselftest: {len(results)} case(s), {len(failures)} hard failure(s)")

    if args.append_evidence:
        from evidence_log import append_block
        lines = [f"- key: {key}",
                 f"- upload_id: {upload_id}",
                 f"- part probe bytes: {PROBE_PART_BYTES} (decimal)",
                 f"- bind_content_length tested: {[True] + ([False] if args.test_unbound else [])}"]
        for r in results:
            verdict = "PASS" if r["pass"] else ("SKIP" if r["pass"] is None else "FAIL")
            lines.append(f"- {verdict}: {r['case']} → expected {r['expected']}, "
                         f"got {r['outcome']} status={r['status']} {r['detail']}")
        unbound_oversized = [r for r in results if "UNBOUND" in r["case"] and "oversized" in r["case"]]
        if unbound_oversized and unbound_oversized[0]["pass"] is False:
            lines.append("- FINDING: without Content-Length binding COS ACCEPTED an oversized part — "
                         "per §3.1 this means the signed plan is not a hard byte limit; candidate A "
                         "must bind Content-Length (or an equivalent hard limit) or it fails §3.0 A-2.")
        append_block(Path(args.append_evidence), "候选 A presign_parts selftest", lines)
        print(f"evidence appended: {args.append_evidence}")
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="print decimal part plan for a declared size")
    p_plan.add_argument("--size", type=int, required=True, help="declared size, decimal bytes")
    p_plan.add_argument("--part-bytes", type=int, default=DEFAULT_PART_BYTES,
                        help=f"part size decimal bytes (default {DEFAULT_PART_BYTES}; "
                             f"contract §3.1's literal 32 MiB is 33554432)")
    p_plan.set_defaults(func=cmd_plan)

    p_sign = sub.add_parser("sign", help="build one UploadPart presigned URL")
    p_sign.add_argument("--key", required=True)
    p_sign.add_argument("--upload-id", required=True)
    p_sign.add_argument("--part-number", type=int, required=True)
    p_sign.add_argument("--content-length", type=int, default=None,
                        help="bind Content-Length into the signature when feasible")
    p_sign.add_argument("--no-bind", action="store_true", help="do not bind Content-Length")
    p_sign.add_argument("--expires", type=int, default=600)
    p_sign.add_argument("--stdout", action="store_true",
                        help="print the real URL to the terminal only (never redirect to a file)")
    p_sign.set_defaults(func=cmd_sign)

    p_self = sub.add_parser("selftest", help="negative suite against real COS (needs operator creds)")
    p_self.add_argument("--fast", action="store_true", help="skip the ~70s expiry-replay wait")
    p_self.add_argument("--test-unbound", action="store_true",
                        help="also measure the unbound variant (records a finding if oversized parts are accepted)")
    p_self.add_argument("--append-evidence", default=str(DEFAULT_EVIDENCE))
    p_self.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
