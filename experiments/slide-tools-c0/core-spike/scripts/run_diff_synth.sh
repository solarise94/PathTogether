#!/bin/bash
# C0 ② differential vs Python oracle on synthetic fixtures (kfb/fixture.py).
# Usage: run_diff_synth.sh [WxH ...]   (default set below)
set -u
REPO=/home/solarise/ZCodeProject/histopilot-suite/PathTogether
SPIKE=$REPO/experiments/slide-tools-c0/core-spike
GATE=$REPO/.gate-tmp/slide-tools-c0/core
PY=$REPO/.venv/bin/python
BIN=$SPIKE/target/release/kfb2tiff
export TMPDIR=$REPO/.gate-tmp
export PYTHONDONTWRITEBYTECODE=1
cd "$REPO"
mkdir -p "$GATE/diff"

SIZES="${*:-580x300 512x512 767x513 1024x768 300x580}"
fail=0
for size in $SIZES; do
  W=${size%x*}; H=${size#*x}
  tag="${W}x${H}"
  echo "=== fixture $tag"
  k="$GATE/diff/fix-$tag.kfb"
  "$PY" - "$W" "$H" "$k" <<'EOF'
import sys
from kfb.fixture import build_synthetic_kfb
build_synthetic_kfb(sys.argv[3], width=int(sys.argv[1]), height=int(sys.argv[2]))
EOF
  rm -f "$GATE/diff/fix-$tag.oracle.tif"
  "$PY" - "$k" "$GATE/diff/fix-$tag.oracle.tif" <<'EOF'
import sys
from kfb.converter import convert_kfb
convert_kfb(sys.argv[1], sys.argv[2], overwrite=True)
EOF
  echo "oracle rc=$?"
  /usr/bin/time -f "spike wall=%es rss=%MkB" "$BIN" convert "$k" "$GATE/diff/fix-$tag.spike.tif" --overwrite > "$GATE/diff/fix-$tag.spike.log" || { echo "spike convert FAILED"; fail=1; continue; }
  cat "$GATE/diff/fix-$tag.spike.log" | head -3
  (cd "$REPO" && PYTHONPATH="$REPO" "$PY" "$SPIKE/scripts/diff_oracle.py" "$GATE/diff/fix-$tag.oracle.tif" "$GATE/diff/fix-$tag.spike.tif" --source "$k" --json "$GATE/diff/fix-$tag.diff.json") || fail=1
  # whole-file byte equality (only expected when every tile is full)
  sha256sum "$GATE/diff/fix-$tag.oracle.tif" "$GATE/diff/fix-$tag.spike.tif" | awk '{print $1}' | uniq -c | awk '{ if ($1 == 2) print "whole-file sha256 EQUAL"; else print "whole-file sha256 differs (expected when edge tiles exist)" }'
done
exit $fail
