#!/bin/bash
# C0 ② differential on the real brightfield sample (alias KFB-1).
# Privacy: the sample path stays under ../切片文件夹; only aliases+sha256 in reports.
set -u
REPO=/home/solarise/ZCodeProject/histopilot-suite/PathTogether
SPIKE=$REPO/experiments/slide-tools-c0/core-spike
GATE=$REPO/.gate-tmp/slide-tools-c0/core
PY=$REPO/.venv/bin/python
BIN=$SPIKE/target/release/kfb2tiff
SRC="/home/solarise/ZCodeProject/histopilot-suite/切片文件夹/26-005817_2026-08-25_14_12_12.kfb"
export TMPDIR=$REPO/.gate-tmp
export PYTHONDONTWRITEBYTECODE=1
cd "$REPO"
mkdir -p "$GATE/kfb1"

echo "=== KFB-1 sha256"
sha256sum "$SRC" | tee "$GATE/kfb1/source.sha256"

echo "=== probe"
"$BIN" probe "$SRC" | tee "$GATE/kfb1/spike-probe.txt"

echo "=== python oracle convert"
rm -f "$GATE/kfb1/KFB-1.oracle.tif"
/usr/bin/time -v -o "$GATE/kfb1/oracle.time.txt" \
  "$PY" - "$SRC" "$GATE/kfb1/KFB-1.oracle.tif" <<'EOF'
import sys, json
from kfb.converter import convert_kfb
m = convert_kfb(sys.argv[1], sys.argv[2], overwrite=True)
print(json.dumps({"keys": sorted(m.keys())[:8], "n_levels": len(m.get("levels", [])), "warnings": m.get("warnings", [])}, ensure_ascii=True))
EOF
grep -E "Elapsed|Maximum resident" "$GATE/kfb1/oracle.time.txt"

echo "=== spike convert"
/usr/bin/time -v -o "$GATE/kfb1/spike.time.txt" \
  "$BIN" convert "$SRC" "$GATE/kfb1/KFB-1.spike.tif" --overwrite > "$GATE/kfb1/spike.log"
rc=$?
grep -E "Elapsed|Maximum resident" "$GATE/kfb1/spike.time.txt"
head -5 "$GATE/kfb1/spike.log"
echo "spike rc=$rc"

echo "=== differential"
PYTHONPATH="$REPO" "$PY" "$SPIKE/scripts/diff_oracle.py" \
  "$GATE/kfb1/KFB-1.oracle.tif" "$GATE/kfb1/KFB-1.spike.tif" \
  --source "$SRC" --json "$GATE/kfb1/KFB-1.diff.json"
echo "diff rc=$?"

echo "=== associated sidecars"
ls "$GATE/kfb1/KFB-1.oracle.tif.associated" "$GATE/kfb1/KFB-1.spike.tif.associated"
for f in overview label thumbnail; do
  a="$GATE/kfb1/KFB-1.oracle.tif.associated/$f.jpg"
  b="$GATE/kfb1/KFB-1.spike.tif.associated/$f.jpg"
  if [ -f "$a" ] && [ -f "$b" ]; then
    if cmp -s "$a" "$b"; then echo "$f: byte-equal"; else echo "$f: DIFFER"; fi
  fi
done
