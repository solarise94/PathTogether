#!/usr/bin/env bash
# C2 cgroup-simulated memory runs (labeled 「cgroup 模拟」 in the report):
# wraps run_memory.js in a user-scope memory-limited cgroup and samples
# memory.events (oom counters) + memory.current live.
#
#   bash run_mem_cgroup.sh <label> <MemoryMax> <size> <profile>
#   e.g. bash run_mem_cgroup.sh cg4g-1g 4G 1g saver
set -euo pipefail
LABEL="$1"; MAX="$2"; SIZE="$3"; PROFILE="$4"
HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="$HERE/../../../.gate-tmp/slide-tools-c2/browser"
UNIT="c2mem-$(echo "$LABEL" | tr -c 'a-zA-Z0-9-' -)"

systemd-run --user --scope --unit "$UNIT" -p MemoryMax="$MAX" -p MemorySwapMax=0 \
  env CGROUP_DESC="cgroup 模拟 MemoryMax=$MAX SwapMax=0" \
  node "$HERE/run_memory.js" --label "$LABEL" --size "$SIZE" --profile "$PROFILE" &
RUN_PID=$!

# locate the scope cgroup and sample it until the run exits
CG=""
for i in $(seq 1 120); do
  # user scopes land under app.slice (observed on this host)
  CG=$(find /sys/fs/cgroup/user.slice -maxdepth 6 -type d -name "${UNIT}.scope" 2>/dev/null | head -1 || true)
  [ -n "$CG" ] && break
  sleep 0.5
done
SAMPLES="$GATE/mem/${LABEL}-cgroup-samples.log"
: > "$SAMPLES" || SAMPLES=/dev/null
if [ -n "$CG" ]; then
  {
    echo "# cgroup=$CG MemoryMax=$MAX"
    while kill -0 "$RUN_PID" 2>/dev/null; do
      echo "$(date +%s.%N) memory.current=$(cat "$CG/memory.current" 2>/dev/null) events=$(cat "$CG/memory.events" 2>/dev/null | tr '\n' ' ')"
      sleep 2
    done
    echo "final events=$(cat "$CG/memory.events" 2>/dev/null | tr '\n' ' ')"
  } >> "$SAMPLES" 2>/dev/null || true
fi
wait "$RUN_PID"
echo "cgroup run $LABEL done (samples: $SAMPLES)"
