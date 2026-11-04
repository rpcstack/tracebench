#!/usr/bin/env bash
# Regenerate the committed deployment for the default graph:
#   docker-compose.yaml            (repository root)
#   deploy/default/{deployment.json,frontend.json,k8s.yaml,k8s-loadgen.yaml}
# For another graph:
#   uv run --project tools tracebench-deploy config/mygraph.json --out deploy/mygraph
set -euo pipefail
cd "$(dirname "$0")/.."
uv run --project tools tracebench-deploy config/default.json \
  --out deploy/default --compose-out docker-compose.yaml
