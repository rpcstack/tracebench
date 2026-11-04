#!/usr/bin/env bash
# Build the tracebench image for MODE, push it to the local registry and deploy
# the generated Kubernetes manifests of a graph deployment.
#
#   scripts/k8s-deploy.sh [baseline|masa]
#
# Expects the cluster from scripts/kind-up.sh (or any cluster that can pull from
# $REGISTRY). Environment: KIND_CLUSTER, REGISTRY_PORT (default 5001), REGISTRY
# (default localhost:$REGISTRY_PORT), DEPLOY_DIR (default deploy/default, see
# scripts/gen-all.sh), SKIP_BUILD=1 to reuse the pushed image.
set -euo pipefail
MODE="${1:-baseline}"
case "$MODE" in baseline|masa) ;; *) echo "usage: $0 [baseline|masa]" >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${KIND_CLUSTER:-rpcstack-tracebench}"
REGISTRY="${REGISTRY:-localhost:${REGISTRY_PORT:-5001}}"
DEPLOY_DIR="${DEPLOY_DIR:-$ROOT/deploy/default}"
IMAGE="$REGISTRY/rpcstack-tracebench:$MODE"
NS=rpcstack-tracebench
export MODE REGISTRY
kc() { kubectl --context "kind-$CLUSTER" "$@"; }

if [ "${SKIP_BUILD:-0}" != 1 ]; then
  echo "== build $IMAGE"
  docker build --build-arg MODE="$MODE" ${BUILD_JOBS:+--build-arg BUILD_JOBS=$BUILD_JOBS} \
    -t "$IMAGE" "$ROOT"
  docker push "$IMAGE"
fi

echo "== apply $DEPLOY_DIR/k8s.yaml ($IMAGE)"
"$ROOT/scripts/lib/render-k8s.sh" "$DEPLOY_DIR/k8s.yaml" | kc apply -f -
# Recreate all pods at once so a re-pushed tag or changed config is picked up
# and no new pod keeps a connection to an old, terminating one.
kc -n "$NS" delete pod -l app.kubernetes.io/component=service --wait=false > /dev/null
for d in $(kc -n "$NS" get deployment -o name); do
  kc -n "$NS" rollout status "$d" --timeout=600s > /dev/null
done
# Wait until only the current pods remain (none terminating).
want=$(kc -n "$NS" get deployment -l app.kubernetes.io/component=service -o name | wc -l)
for _ in $(seq 1 120); do
  have=$(kc -n "$NS" get pods -l app.kubernetes.io/component=service --no-headers | wc -l)
  [ "$have" -eq "$want" ] && break
  sleep 2
done
echo "deployed $IMAGE ($want services) to namespace $NS"
