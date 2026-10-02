#!/usr/bin/env bash
# QuPath default-reader acceptance probe (see docs/slide-tools/bf-ome-acceptance-report.md §4).
#
# usage: run_probe.sh <qupath-root> <out-dir> <image> [tissue-fx tissue-fy] \
#                     [extra check_probe.py flags...]
#   e.g. run_probe.sh /opt/QuPath out img.ome.tif 0.5 0.5 \
#                        --expect-server-substring BioFormats --expect-rgb
#   follow-up pixel comparison:
#   python compare_regions.py <out-dir>/probe.json <out-dir>/regions <classic.tif> <out-dir>/region-compare.json
#   OME-XML validation:
#   <qupath-root>/bin/QuPath script xsd-validate.groovy -a <ome.xsd> -a <ome.xml>...
#
# Uses the QuPath launcher's own JVM; no reader is forced and no preference is
# changed. Writes <out-dir>/probe.json, <out-dir>/regions/*.png,
# <out-dir>/project/, <out-dir>/probe.log.
#
# Exit status:
#   0    QuPath exited 0 AND the evidence passed check_probe.py
#        (probe.json parseable, resolutions/levels consistent, 4 region PNGs
#        per level with the exact box geometry, bioformats + project_reopen
#        present and consistent)
#   nonzero  whatever QuPath/timeout returned, propagated as-is
#        (124 = timeout, 127 = launcher not found); or 2 when QuPath exited 0
#        but the evidence is missing, unparseable or incomplete.
# The interpreter used for the evidence checker is ${PYTHON:-python3}.
set -euo pipefail

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

if [ $# -lt 3 ]; then
    echo "run_probe: usage: run_probe.sh <qupath-root> <out-dir> <image> [tissue-fx tissue-fy] [check_probe flags...]" >&2
    usage >&2
    exit 64
fi

E=$(cd "$(dirname "$0")" && pwd)
Q=$1; OUT=$2; IMG=$3
shift 3
FX=0.5; FY=0.5
if [ $# -ge 1 ]; then FX=$1; shift; fi
if [ $# -ge 1 ]; then FY=$1; shift; fi
CHECK_ARGS=("$@")

if [ ! -x "$Q/bin/QuPath" ]; then
    echo "run_probe: FAIL: QuPath launcher not found/executable: $Q/bin/QuPath" >&2
    exit 127
fi
if [ ! -f "$IMG" ]; then
    echo "run_probe: FAIL: image not found: $IMG" >&2
    exit 66
fi

rm -rf "$OUT"; mkdir -p "$OUT"

set +e
timeout 900 "$Q/bin/QuPath" script "$E/default-probe.groovy" -a "$IMG" -a "$OUT/probe.json" \
  -a "$OUT/regions" -a "$OUT/project" -a "$FX" -a "$FY" > "$OUT/probe.log" 2>&1
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
    echo "run_probe: FAIL: QuPath/timeout exited $rc (log: $OUT/probe.log)" >&2
    exit "$rc"
fi

PYTHON=${PYTHON:-python3}
if ! "$PYTHON" "$E/check_probe.py" "$OUT/probe.json" --regions-dir "$OUT/regions" \
      ${CHECK_ARGS[@]+"${CHECK_ARGS[@]}"}; then
    echo "run_probe: FAIL: QuPath exited 0 but the probe evidence is missing/incomplete (see above; log: $OUT/probe.log)" >&2
    exit 2
fi
echo "run_probe: OK: evidence validated in $OUT"
