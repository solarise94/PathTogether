#!/usr/bin/env bash
# Review §1 CLI regression: the reviewer's large-grid negative — a ~51 KB
# synthetic MRXS bundle whose INI declares IMAGENUMBER 5000×5000 with
# CameraImageDivisionsPerSide = 1 and no position buffer (nominal fallback).
#
# Old behaviour: `slide-transform probe` allocates the 25 M position tuples
# (~400 MB) before anything else and is OOM-killed under the saver budget
# (signal 9 / exit 137). Required behaviour: the probe returns the stable
# typed JSON error `resource_profile_insufficient` and exits 1 INSIDE a
# 192 MiB cgroup — the refusal happens before the allocation.
#
# Usage: bash scripts/test_mrxs_memory_budget.sh [path-to-slide-transform]
# (build with: cargo build --release -p slide-transform-cli)
set -euo pipefail

BIN=${1:-slide-transform-core/target/release/slide-transform}
BIN=$(readlink -f "$BIN")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# 1. generate the candidate-delivery synthetic bundle (8×6, two levels,
#    five members, ~51 KB — same as the reviewer's step 1)
OUT=$(dirname "$WORK")
TMPDIR="$OUT" "$BIN" gen-mrxs "$WORK/synthetic" --images-x 8 --images-y 6 --levels 2 >/dev/null
S="$WORK/synthetic/synthetic"

# 2. the reviewer's rewrite: INI → 5000×5000, divisions 1, no position
#    buffer record (NONHIER_1 renamed so nothing matches the supported
#    position sources) — image members and index stay tiny
sed -i \
  -e 's/^IMAGENUMBER_X = 8$/IMAGENUMBER_X = 5000/' \
  -e 's/^IMAGENUMBER_Y = 6$/IMAGENUMBER_Y = 5000/' \
  -e 's/^CameraImageDivisionsPerSide = 2$/CameraImageDivisionsPerSide = 1/' \
  -e 's/^NONHIER_1_NAME = VIMSLIDE_POSITION_BUFFER$/NONHIER_1_NAME = unused/' \
  "$S/Slidedat.ini"
du -sb "$S" | awk '{ printf "bundle bytes: %d\n", $1 }'

run_under_192m() {
  systemd-run --user --scope -q -p MemoryMax=192M -p MemorySwapMax=0 \
    "$BIN" "$@" 2>"$WORK/stderr.log"
}

# 3. probe under the saver budget: typed JSON error, exit 1 — NOT signal 9
set +e
OUT=$(run_under_192m probe "$WORK/synthetic/synthetic.mrxs")
RC=$?
set -e
echo "probe exit: $RC"
echo "probe stdout: $OUT"
if [ "$RC" -ne 1 ]; then
  echo "FAIL: expected exit 1 (typed error), got $RC" >&2
  tail -5 "$WORK/stderr.log" >&2
  exit 1
fi
echo "$OUT" | grep -q '"error":{"code":"resource_profile_insufficient"' || {
  echo "FAIL: stdout is not the typed resource_profile_insufficient error" >&2
  exit 1
}
if grep -q 'KILL' "$WORK/stderr.log"; then
  echo "FAIL: process was killed instead of returning the typed error" >&2
  exit 1
fi

# 4. convert must refuse the same way (probe runs first inside the converter)
set +e
OUT=$(run_under_192m convert "$WORK/synthetic/synthetic.mrxs" "$WORK/out.tif")
RC=$?
set -e
echo "convert exit: $RC"
echo "convert stdout: $OUT"
if [ "$RC" -ne 1 ] || ! echo "$OUT" | grep -q '"error":{"code":"resource_profile_insufficient"'; then
  echo "FAIL: convert must exit 1 with the typed resource_profile_insufficient error" >&2
  exit 1
fi

echo "PASS: probe+convert return the typed resource_profile_insufficient error under MemoryMax=192M"
