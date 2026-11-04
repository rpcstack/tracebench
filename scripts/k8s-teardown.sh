#!/usr/bin/env bash
# Remove the tracebench deployment; with --cluster also delete the kind cluster
# and registry (scripts/kind-down.sh).
set -euo pipefail
CLUSTER="${KIND_CLUSTER:-rpcstack-tracebench}"
kubectl --context "kind-$CLUSTER" delete namespace rpcstack-tracebench --ignore-not-found --wait=true || true
if [ "${1:-}" = --cluster ]; then "$(dirname "$0")/kind-down.sh"; fi
