#!/bin/sh
# Rewrite the image reference in a generated manifest to this run's registry and mode.
#   MODE=masa REGISTRY=localhost:5001 scripts/lib/render-k8s.sh deploy/default/k8s.yaml
sed -e "s#localhost:5001/rpcstack-tracebench:baseline#${REGISTRY:-localhost:5001}/rpcstack-tracebench:${MODE:-baseline}#g" "$@"
