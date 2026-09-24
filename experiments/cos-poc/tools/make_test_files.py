#!/usr/bin/env python3
"""Random test-file generator for the COS direct-upload PoC.

Contract: docs/cos-direct-upload-audit-plan.md §10 (Phase 0 inputs: "Agent 可
准备生成脚本，记录尺寸、生成参数和 SHA-256，无需操作者上传现成文件") and
docs/upload-routing-open-source-review.md §0 D3 (decimal byte discipline:
300 MB == 300000000; NEVER 300*1024*1024).

No medical data: content is random bytes, nothing else.

Sizes are plain decimal integers on the command line:
    --sizes 300000000,500000000          (contract Phase 0 defaults)
    --sizes 2000000000                   (conditional add-on)
    --sizes 9499000000                   (near the 9.5 GB admission boundary)

Generation strategies:
  numpy   — deterministic, chunked PCG64 stream seeded per chunk from a master
            seed (fast; ~GB/s). Reproducible from the recorded seed.
  urandom — chunked os.urandom fallback when numpy is unavailable
            (not reproducible; recorded as such in the manifest).

The manifest records, per file: size, sha256, method, seed and chunk size, so
the audit evidence can cross-check downloads byte-for-byte.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

# Buffer granularity for writing. This is I/O chunking, NOT a file-size unit;
# file sizes are decimal integers supplied by --sizes. 32000000 == 32 MB decimal.
CHUNK_BYTES = 32_000_000

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "testfiles"

DEFAULT_SIZES = "300000000,500000000"


def parse_sizes(raw: str) -> List[int]:
    sizes: List[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise SystemExit(
                f"invalid size {token!r}: sizes must be plain decimal integers "
                f"(e.g. 300000000); binary MiB/GiB arithmetic is forbidden (D3)"
            )
        n = int(token)
        if n <= 0:
            raise SystemExit(f"invalid size {token!r}: must be > 0")
        sizes.append(n)
    if not sizes:
        raise SystemExit("no sizes given")
    return sizes


def chunk_seed(master_seed: int, size: int, index: int) -> int:
    """Derive a stable per-chunk seed from the master seed and file identity."""
    material = f"cos-poc:{master_seed}:{size}:{index}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def numpy_chunks(total: int, master_seed: int, size: int):
    import numpy as np  # imported lazily; optional dependency

    remaining = total
    index = 0
    while remaining > 0:
        take = min(CHUNK_BYTES, remaining)
        rng = np.random.default_rng(chunk_seed(master_seed, size, index))
        yield rng.bytes(take)
        remaining -= take
        index += 1


def urandom_chunks(total: int):
    remaining = total
    while remaining > 0:
        take = min(CHUNK_BYTES, remaining)
        yield os.urandom(take)
        remaining -= take


def write_file(path: Path, size: int, master_seed: Optional[int]) -> dict:
    sha = hashlib.sha256()
    started = time.time()
    used_numpy = False
    try:
        import numpy  # noqa: F401

        used_numpy = True
        chunks = numpy_chunks(size, master_seed if master_seed is not None else 0, size)
        method = "numpy-pcg64-chunked"
    except ImportError:
        chunks = urandom_chunks(size)
        method = "os.urandom-chunked"
    written = 0
    with open(path, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
            sha.update(chunk)
            written += len(chunk)
    if written != size:
        raise SystemExit(f"size mismatch for {path.name}: wrote {written}, expected {size}")
    record = {
        "filename": path.name,
        "size_bytes": size,
        "sha256": sha.hexdigest(),
        "method": method,
        "chunk_bytes": CHUNK_BYTES,
        "master_seed": master_seed if used_numpy else None,
        "reproducible": used_numpy and master_seed is not None,
        "duration_seconds": round(time.time() - started, 3),
        "created_at_unix": int(time.time()),
    }
    if not used_numpy:
        record["note"] = "os.urandom content is NOT reproducible; sha256 is the only integrity anchor"
    return record


def verify(out_dir: Path, manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = 0
    for entry in manifest["files"]:
        path = out_dir / entry["filename"]
        if not path.is_file():
            print(f"FAIL {entry['filename']}: missing")
            failures += 1
            continue
        actual_size = path.stat().st_size
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(CHUNK_BYTES), b""):
                sha.update(block)
        problems = []
        if actual_size != entry["size_bytes"]:
            problems.append(f"size {actual_size} != manifest {entry['size_bytes']}")
        if sha.hexdigest() != entry["sha256"]:
            problems.append("sha256 mismatch")
        if problems:
            print(f"FAIL {entry['filename']}: {'; '.join(problems)}")
            failures += 1
        else:
            print(f"OK   {entry['filename']}: size={actual_size} sha256 matches manifest")
    print(f"verify: {len(manifest['files'])} file(s), {failures} failure(s)")
    return 1 if failures else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sizes", default=DEFAULT_SIZES,
                        help=f"comma-separated decimal byte sizes (default {DEFAULT_SIZES})")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT), help="output directory (default: <poc>/data/testfiles)")
    parser.add_argument("--seed", type=int, default=None,
                        help="master seed for the numpy generator (default: random, recorded in manifest)")
    parser.add_argument("--manifest", default=None,
                        help="manifest path (default: <out-dir>/manifest.json)")
    parser.add_argument("--verify", action="store_true",
                        help="re-hash files against the manifest instead of generating")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    manifest_path = Path(args.manifest).resolve() if args.manifest else out_dir / "manifest.json"

    if args.verify:
        if not manifest_path.is_file():
            raise SystemExit(f"manifest not found: {manifest_path}")
        return verify(out_dir, manifest_path)

    out_dir.mkdir(parents=True, exist_ok=True)
    master_seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(8), "big")
    sizes = parse_sizes(args.sizes)

    manifest = {
        "generator": "experiments/cos-poc/tools/make_test_files.py",
        "byte_unit": "decimal",
        "chunk_bytes": CHUNK_BYTES,
        "master_seed": master_seed,
        "files": [],
    }
    for size in sizes:
        name = f"poc-random-{size}.bin"
        record = write_file(out_dir / name, size, master_seed)
        manifest["files"].append(record)
        print(f"wrote {name}: {size} bytes via {record['method']} sha256={record['sha256']}")

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
