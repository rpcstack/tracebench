#!/usr/bin/env bash
# End-to-end smoke test of the Kubernetes deployment (same checks as
# scripts/smoke-compose.sh, run as Jobs inside the cluster).
#
#   scripts/smoke-k8s.sh [baseline|masa]
#
# Creates the kind cluster + registry if needed (scripts/kind-up.sh), deploys
# MODE (scripts/k8s-deploy.sh), then runs the policy-liveness probe, moderate
# load, an overload burst and a recovery run. Environment: KIND_CLUSTER,
# In baseline the probe also runs with --expect masa as a negative control
# that must FAIL (PROBE FAIL).
# REGISTRY_PORT, SKIP_DEPLOY=1, KEEP_UP=1 (otherwise the namespace is deleted;
# the cluster is kept, remove it with scripts/kind-down.sh).
set -euo pipefail
MODE="${1:-baseline}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${KIND_CLUSTER:-rpcstack-tracebench}"
REGISTRY="${REGISTRY:-localhost:${REGISTRY_PORT:-5001}}"
IMAGE="$REGISTRY/rpcstack-tracebench:$MODE"
DEPLOY_DIR="${DEPLOY_DIR:-$ROOT/deploy/default}"
NS=rpcstack-tracebench
MIN_OK="${SMOKE_MIN_OK_RATE:-0.99}"
OUT="${SMOKE_OUT:-$ROOT/out/smoke-k8s-$MODE}"
mkdir -p "$OUT"
kc() { kubectl --context "kind-$CLUSTER" "$@"; }
SELECTOR=app.kubernetes.io/component=service

cleanup() {
  status=$?
  kc -n "$NS" logs -l "$SELECTOR" --all-containers --tail=-1 --prefix > "$OUT/pods.log" 2>&1 || true
  if [ "${KEEP_UP:-0}" != 1 ]; then kc delete namespace "$NS" --ignore-not-found --wait=true > /dev/null 2>&1 || true; fi
  [ $status -eq 0 ] && echo "SMOKE PASS ($MODE, k8s)" || echo "SMOKE FAIL ($MODE, k8s), logs in $OUT"
  exit $status
}

if [ "${SKIP_DEPLOY:-0}" != 1 ]; then
  "$ROOT/scripts/kind-up.sh"
  "$ROOT/scripts/k8s-deploy.sh" "$MODE"
fi
trap cleanup EXIT

WAIT_FRONTENDS=$(python3 "$ROOT/scripts/lib/probe_args.py" "$DEPLOY_DIR/deployment.json" --wait-frontends)
# shellcheck disable=SC2207
PROBE_ARGS=($(python3 "$ROOT/scripts/lib/probe_args.py" "$DEPLOY_DIR/deployment.json"))

# run_job NAME [ENV=VALUE...] -- ARGS... : run the image as a Job, print its log.
run_job() {
  local name="$1"; shift
  local env=""
  while [ "$1" != -- ]; do env="$env{name: ${1%%=*}, value: \"${1#*=}\"}, "; shift; done
  shift
  local args; args=$(printf '"%s",' "$@"); args="[${args%,}]"
  kc -n "$NS" delete job "$name" --ignore-not-found --wait=true > /dev/null
  kc -n "$NS" apply -f - > /dev/null <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: $name
          image: $IMAGE
          args: $args
          env: [${env%, }]
          volumeMounts: [{name: config, mountPath: /etc/tracebench, readOnly: true}]
      volumes:
        - name: config
          configMap: {name: tb-config}
YAML
  local phase=""
  for _ in $(seq 1 600); do
    phase=$(kc -n "$NS" get job "$name" -o jsonpath='{.status.conditions[?(@.status=="True")].type}' 2>/dev/null || true)
    case "$phase" in *Complete*|*Failed*) break ;; esac
    sleep 1
  done
  kc -n "$NS" logs "job/$name"
  case "$phase" in *Complete*) return 0 ;; *) echo "job $name: ${phase:-timed out}" >&2; return 1 ;; esac
}
check() { python3 "$ROOT/scripts/lib/check_traces.py" "$@"; }
restarts() {
  kc -n "$NS" get pods -l "$SELECTOR" \
    -o jsonpath='{range .items[*]}{.metadata.name} {.status.containerStatuses[0].restartCount}{"\n"}{end}' | sort
}

echo "image mode: $(run_job tb-mode -- mode)"
run_probe() {
  run_job "$1" -- probe --expect "$2" --wait-secs 300 "${PROBE_ARGS[@]}"
}
echo "== policy-liveness probe (expect $MODE)"
run_probe tb-probe "$MODE" | tee "$OUT/probe.txt"
if [ "$MODE" = baseline ]; then
  # Negative control: the masa expectation must fail against a baseline stack.
  echo "== negative control: probe --expect masa must FAIL on baseline"
  if run_probe tb-probe-negative masa > "$OUT/probe-negative.txt" 2>&1; then
    cat "$OUT/probe-negative.txt"; echo "FAIL: masa probe passed against baseline stack" >&2; exit 1
  fi
  grep -q "PROBE FAIL" "$OUT/probe-negative.txt" || { cat "$OUT/probe-negative.txt"; echo "FAIL: negative probe did not report PROBE FAIL" >&2; exit 1; }
  echo "negative control ok"
fi
restarts > "$OUT/restarts.before"

load() { # name -> config/smoke/<name>.env; traces in $OUT/<name>.txt
  local envs; envs=$(grep -v '^#' "$ROOT/config/smoke/$1.env" | grep .)
  # shellcheck disable=SC2086
  run_job "tb-load-$1" $envs "FRONTEND_CONFIG=/etc/tracebench/frontend.json" \
    "WAIT_FRONTENDS=$WAIT_FRONTENDS" -- loadgen > "$OUT/$1.txt"
}

echo "== moderate load"
load moderate
extra=(--max-rejected 0); [ "$MODE" = masa ] && extra=()
check "$OUT/moderate.txt" --label moderate --min-requests 1000 --min-ok-rate "$MIN_OK" --max-transport 0 "${extra[@]}"

if [ "$MODE" = masa ]; then
  echo "== overload burst"
  load overload
  check "$OUT/overload.txt" --label overload --min-rejected 1

  echo "== recovery"
  load recovery
  check "$OUT/recovery.txt" --label recovery --tail-secs 5 --min-requests 300 --min-ok-rate "$MIN_OK" --max-transport 0
elif [ "${SMOKE_OVERLOAD:-0}" = 1 ]; then
  echo "== overload burst (baseline, report only)"
  load overload
  check "$OUT/overload.txt" --label overload --max-rejected 0
  load recovery
  check "$OUT/recovery.txt" --label recovery --tail-secs 5 || true
fi

echo "== probe again (policy state and control requests after the load runs)"
run_probe tb-probe-after "$MODE" | tee "$OUT/probe-after-overload.txt"

echo "== crash check"
kc -n "$NS" logs -l "$SELECTOR" --all-containers --tail=-1 --prefix > "$OUT/pods.log"
if grep -n "panicked" "$OUT/pods.log"; then echo "FAIL: panic in pod logs"; exit 1; fi
restarts > "$OUT/restarts.after"
if ! diff "$OUT/restarts.before" "$OUT/restarts.after"; then echo "FAIL: pods restarted during the smoke"; exit 1; fi
echo "no panics, no restarts"
