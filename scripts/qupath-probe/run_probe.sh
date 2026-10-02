#!/usr/bin/env bash
# QuPath default-reader acceptance probe (see docs/slide-tools/bf-ome-acceptance-report.md §4).
#
#   run_probe.sh <qupath-root> <out-dir> <image> [tissue-fx tissue-fy]
#   python compare_regions.py <out-dir>/probe.json <out-dir>/regions <classic.tif> <out-dir>/region-compare.json
#   <qupath-root>/bin/QuPath script xsd-validate.groovy -a <ome.xsd> -a <ome.xml>...
#
# Uses the QuPath launcher's own JVM; no reader is forced and no preference is changed.
set -u
E=$(cd "$(dirname "$0")" && pwd)
Q=$1; OUT=$2; IMG=$3; FX=${4:-0.5}; FY=${5:-0.5}
rm -rf "$OUT"; mkdir -p "$OUT"
timeout 900 "$Q/bin/QuPath" script "$E/default-probe.groovy" -a "$IMG" -a "$OUT/probe.json" \
  -a "$OUT/regions" -a "$OUT/project" -a "$FX" -a "$FY" > "$OUT/probe.log" 2>&1
echo "exit=$?"
