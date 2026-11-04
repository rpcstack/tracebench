#!/usr/bin/env bash
# End-to-end smoke test of the Docker Compose deployment (default graph).
#
#   scripts/smoke-compose.sh [baseline|masa]
#
# 1. Build the image for MODE and bring up every service of the graph (no load
#    generator).
# 2. Policy-liveness probe (tracebench-probe): readiness wait, control Root
#    requests through the whole graph, then one expired-deadline request per
#    service. In masa mode every service must answer with Masa's /EarlyReturn;
#    in baseline none may.
# 3. Moderate load (config/smoke/moderate.env): zero transport errors, ok rate
#    >= SMOKE_MIN_OK_RATE (default 0.99); baseline must show zero Masa rejections.
# 4. masa only: overload burst (config/smoke/overload.env) must be shed with
#    >= 1 /EarlyReturn; recovery (config/smoke/recovery.env) must reach ok rate
#    >= SMOKE_MIN_OK_RATE with zero transport errors in its last 5 s.
#    SMOKE_OVERLOAD=1 also runs the burst in baseline, report only.
# 5. The probe runs again, then: no panic in any log, no container restart.
#
# Environment: COMPOSE_PROJECT_NAME (default rpcstack-tracebench-<mode>),
# In baseline the probe also runs with --expect masa as a negative control
# that must FAIL (PROBE FAIL).
# KEEP_UP=1 to leave the stack running, SKIP_BUILD=1 to reuse the image.
set -euo pipefail

MODE="${1:-${MODE:-baseline}}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2 ;; esac
export MODE
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-rpcstack-tracebench-$MODE}"
MIN_OK="${SMOKE_MIN_OK_RATE:-0.99}"
OUT="${SMOKE_OUT:-$ROOT/out/smoke-compose-$MODE}"
mkdir -p "$OUT"
dc() { docker compose "$@"; }

cleanup() {
  status=$?
  dc logs --no-color > "$OUT/compose.log" 2>&1 || true
  if [ "${KEEP_UP:-0}" != 1 ]; then dc down -v --remove-orphans > /dev/null 2>&1 || true; fi
  [ $status -eq 0 ] && echo "SMOKE PASS ($MODE)" || echo "SMOKE FAIL ($MODE), logs in $OUT"
  exit $status
}
trap cleanup EXIT

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  echo "== build ($MODE)"
  dc build
fi
echo "== up"
dc up -d --scale loadgen=0
echo "image mode: $(dc run --rm --no-deps loadgen mode)"

# shellcheck disable=SC2046
probe() {
  local expect="$1"; shift
  dc run --rm --no-deps loadgen probe --expect "$expect" --wait-secs 300 \
    $(python3 "$ROOT/scripts/lib/probe_args.py" "$ROOT/deploy/default/deployment.json") "$@"
}

echo "== policy-liveness probe (expect $MODE)"
probe "$MODE" | tee "$OUT/probe.txt"
if [ "$MODE" = baseline ]; then
  # Negative control: the masa expectation must fail against a baseline stack,
  # otherwise the probe cannot tell the two modes apart.
  echo "== negative control: probe --expect masa must FAIL on baseline"
  if probe masa > "$OUT/probe-negative.txt" 2>&1; then
    cat "$OUT/probe-negative.txt"; echo "FAIL: masa probe passed against baseline stack" >&2; exit 1
  fi
  grep -q "PROBE FAIL" "$OUT/probe-negative.txt" || { cat "$OUT/probe-negative.txt"; echo "FAIL: negative probe did not report PROBE FAIL" >&2; exit 1; }
  echo "negative control ok"
fi

load() { # name -> runs config/smoke/<name>.env, traces in $OUT/<name>.txt
  set -a; . "$ROOT/config/smoke/$1.env"; set +a
  dc run --rm --no-deps -e RPS -e DURATION -e WARMUP_SEC -e MAX_IN_FLIGHT loadgen loadgen \
    > "$OUT/$1.txt" 2> "$OUT/$1.stderr" || { tail -20 "$OUT/$1.stderr"; return 1; }
}
check() { python3 "$ROOT/scripts/lib/check_traces.py" "$@"; }

echo "== moderate load"
load moderate
if [ "$MODE" = masa ]; then
  check "$OUT/moderate.txt" --label moderate --min-requests 1000 --min-ok-rate "$MIN_OK" --max-transport 0
else
  check "$OUT/moderate.txt" --label moderate --min-requests 1000 --min-ok-rate "$MIN_OK" --max-transport 0 --max-rejected 0
fi

if [ "$MODE" = masa ]; then
  echo "== overload burst"
  load overload
  check "$OUT/overload.txt" --label overload --min-rejected 1

  echo "== recovery"
  load recovery
  # Masa's predictive admission (AIMD) reopens only as admitted requests
  # complete, so judge recovery on the last 5 s of a 20 s run at 100 rps.
  check "$OUT/recovery.txt" --label recovery --tail-secs 5 --min-requests 300 --min-ok-rate "$MIN_OK" --max-transport 0
elif [ "${SMOKE_OVERLOAD:-0}" = 1 ]; then
  # Report only: without admission control the overload reaches the backends
  # and baseline is not expected to recover within the run (see README).
  echo "== overload burst (baseline, report only)"
  load overload
  check "$OUT/overload.txt" --label overload --max-rejected 0
  load recovery
  check "$OUT/recovery.txt" --label recovery --tail-secs 5 || true
fi

echo "== probe again (policy state and control requests after the load runs)"
probe "$MODE" | tee "$OUT/probe-after-overload.txt"

echo "== crash check"
dc logs --no-color > "$OUT/compose.log" 2>&1
if grep -n "panicked" "$OUT/compose.log"; then echo "FAIL: panic in service logs"; exit 1; fi
restarts=$(docker inspect -f '{{.Name}} {{.RestartCount}}' $(dc ps -q) | awk '$2 > 0')
if [ -n "$restarts" ]; then echo "FAIL: restarted containers: $restarts"; exit 1; fi
echo "no panics, no restarts"
