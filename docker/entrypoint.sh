#!/bin/sh
# Select a tracebench binary.
#
#   service            generic-service. Environment (set by the generated
#                      deployment): SERVICE_NAME, SERVICE_PORT,
#                      DEPLOYMENT_CONFIG_PATH, CALLGRAPH_DIRS (comma-separated
#                      graph directories relative to /app, e.g.
#                      trace-analysis/graph_branch/S_14677443).
#   loadgen            load generator. Environment: FRONTEND_CONFIG (frontend.json),
#                      RPS, DURATION, WARMUP_SEC, MAX_IN_FLIGHT,
#                      STATS_INTERVAL_SEC, WAIT_FRONTENDS (graph=http://host:port,...).
#                      Prints the per-request CSV traces after the run.
#   probe [args...]    policy-liveness probe (see tracebench-probe)
#   mode               print the policy mode baked into this image
set -eu

cmd="${1:-}"
[ $# -gt 0 ] && shift

case "$cmd" in
  service)
    : "${CALLGRAPH_DIRS:?CALLGRAPH_DIRS is required}"
    # Each service reads every directory under /app/callgraphs; the directory
    # name is the graph id.
    mkdir -p /app/callgraphs
    old_ifs="$IFS"; IFS=','
    for d in $CALLGRAPH_DIRS; do
      [ -d "/app/$d" ] || { echo "tracebench: graph directory /app/$d not found" >&2; exit 1; }
      ln -sfn "/app/$d" "/app/callgraphs/$(basename "$d")"
    done
    IFS="$old_ifs"
    echo "tracebench: starting $SERVICE_NAME ($(cat /etc/tracebench-mode)), graphs: $CALLGRAPH_DIRS" >&2
    exec /usr/local/bin/generic-service "$@"
    ;;
  loadgen)
    cp "${FRONTEND_CONFIG:-/etc/tracebench/frontend.json}" /app/frontend.json
    if [ -n "${WAIT_FRONTENDS:-}" ]; then
      args=""
      old_ifs="$IFS"; IFS=','
      for f in $WAIT_FRONTENDS; do args="$args --frontend $f"; done
      IFS="$old_ifs"
      # Readiness only: the probe's control requests are real Root RPCs.
      # shellcheck disable=SC2086
      /usr/local/bin/tracebench-probe --ready-only \
        --wait-secs "${WAIT_SECS:-300}" $args >&2
    fi
    status=0
    /usr/local/bin/tracebench-loadgen "$@" || status=$?
    # Emit the per-request traces so callers can collect them from the logs.
    echo "---BEGIN TRACES---"
    for f in /app/loadgen_output/*.csv; do
      [ -f "$f" ] || continue
      echo "FILE: $(basename "$f")"
      cat "$f"
      echo "---END FILE---"
    done
    echo "---END TRACES---"
    exit $status
    ;;
  probe)
    exec /usr/local/bin/tracebench-probe "$@"
    ;;
  mode)
    cat /etc/tracebench-mode
    ;;
  *)
    echo "usage: tracebench-entrypoint service|loadgen|probe|mode [args...]" >&2
    exit 2
    ;;
esac
