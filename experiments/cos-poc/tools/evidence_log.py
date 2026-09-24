"""Shared, redaction-safe evidence appending for the COS PoC.

The unified block format (README §证据格式) is produced here so every tool
(bench, presign selftest, sts issuer) writes identical, diff-friendly blocks
into docs/evidence/cos-20260924.md.

Redaction gate: refuse to append content containing secret-shaped strings
(SecretId/SecretKey/token values, q-signature=..., x-cos-security-token=...).
Passive defense only — callers must still never build such strings.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List

_FORBIDDEN = [
    re.compile(r"q-signature=[0-9a-f]{40}", re.I),
    re.compile(r"x-cos-security-token=[^\s&'\"]+", re.I),
    re.compile(r"q-ak=AKID[A-Za-z0-9]+", re.I),
    re.compile(r"\bAKID[A-Za-z0-9]{20,}\b"),
    re.compile(r"TmpSecret(Key|Id)\s*[:=]\s*['\"]?[A-Za-z0-9/+]{16,}", re.I),
]


def check_redacted(text: str) -> None:
    for pattern in _FORBIDDEN:
        match = pattern.search(text)
        if match:
            raise ValueError(
                f"refusing to write evidence: secret-shaped content matched {pattern.pattern!r}"
            )


def append_block(evidence_path: Path, title: str, lines: List[str]) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = "\n".join(lines)
    check_redacted(body)
    block = f"\n### [AUTO] {title} — {now}\n{body}\n"
    with open(evidence_path, "a", encoding="utf-8") as fh:
        fh.write(block)


def throughput_lines(
    leg: str,
    key: str,
    bytes_per_attempt: List[int],
    seconds_per_attempt: List[float],
    failures: int,
    notes: str = "",
) -> List[str]:
    """Unified three-leg throughput record (audit-plan §7 阶段0 / 分流调研 校准 1)."""
    ok_pairs = [(b, s) for b, s in zip(bytes_per_attempt, seconds_per_attempt) if s > 0 and b > 0]
    mbps = [b * 8 / s / 1_000_000 for b, s in ok_pairs]
    lines = [
        f"- leg: {leg}",
        f"- key: {key}",
        f"- attempts: {len(bytes_per_attempt)}, failures: {failures}",
        f"- bytes: {bytes_per_attempt}",
        f"- seconds: {[round(s, 3) for s in seconds_per_attempt]}",
        f"- Mbps(decimal): {[round(m, 2) for m in mbps]}"
        + (f" | median={round(sorted(mbps)[len(mbps)//2], 2)}" if mbps else " | median=n/a"),
    ]
    if notes:
        lines.append(f"- notes: {notes}")
    return lines
