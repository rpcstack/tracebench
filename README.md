# tracebench

A microservice benchmark that builds a service graph from prepared Alibaba
call-graph directories.

## Application

tracebench instantiates an arbitrary RPC call graph, taken from a prepared
Alibaba trace graph directory, as a set of real gRPC services running on
[rpcstack](https://github.com/rpcstack/rpcstack), plus a load generator.

* `crates/generic-service`: one binary (`generic-service`) plays any node of the
  graph. It reads the graph directories, learns which children it calls, burns
  the sampled latency (busy spin plus sleep) and fans out calls following the
  recorded call sequences. Also builds `tracebench-loadgen` (Poisson arrivals
  against the root services) and `tracebench-probe` (policy-liveness probe).
* `crates/trace-config`: graph directory parsing (edges, interface
  distribution, latency percentiles, conditional call sequences).
* `crates/app-utils`: load generator helpers shared with the other rpcstack apps.
* `tools/`: a small uv project (`tracebench-deploy`) that turns a graph config
  into Docker Compose and Kubernetes files.
* `trace-analysis/`: turns raw traces into graph directories (see below).

Services are named `tb-<service>-<replica>` (for example `tb-ms-56394-1`); each
graph gets its own root service `tb-user-<graph>-1`. All processes use one
locally built image (`rpcstack-tracebench:<mode>`); nothing is pulled or
published.

### Modes

The mode is chosen when the image is built.

| Mode | Cargo feature | Stack |
|---|---|---|
| `baseline` (default) | `baseline` | generated stubs use rpcstack's no-op hooks: no policy |
| `masa` | `masa` | the Masa composed policy preset: `sched_slo, abort_slo, signal_slack, est_mean_var, ac_pred` (see rpcstack's `examples/masa/README.md`) |

Scheduling operates between future polls; there is no forced preemption.

### Deploy in one command

Layout: generated per-graph manifests in `deploy/<graph>/` (`deploy/default`);
no kustomize overlays.

```bash
MODE=masa docker compose up --build                       # Compose (omit MODE for baseline)
scripts/kind-up.sh && scripts/k8s-deploy.sh masa          # Kubernetes (or: baseline)
```

### Quick start: Docker Compose

Needs Docker with Compose v2 and BuildKit. The first build compiles the Rust
workspace (a few minutes). Dependencies build in a separate cargo-chef layer of
the Dockerfile, so later builds reuse it; CI persists that layer in the GitHub
Actions cache.

```bash
docker compose up --build                  # baseline, default graph
MODE=masa docker compose up --build        # Masa policy
```

This starts every service of the default graph
(`trace-analysis/graph_branch/S_14677443`, 8 backend services and one root) and
then the load generator (`config/default.json`: 200 rps for 30 s, SLO 100 ms),
which prints per-second stats and the per-request CSV traces and exits.
Re-run the load with `docker compose run --rm loadgen`, override it with
`-e RPS=500 -e DURATION=60`, and clean up with `docker compose down -v`.

### Quick start: Kubernetes (kind + local registry)

Needs Docker, [kind](https://kind.sigs.k8s.io/) and kubectl.

```bash
scripts/kind-up.sh                          # kind cluster + registry on localhost:5001
scripts/k8s-deploy.sh masa                  # build, push, apply (or: baseline)
kubectl --context kind-rpcstack-tracebench -n rpcstack-tracebench get pods
kubectl --context kind-rpcstack-tracebench apply -f deploy/default/k8s-loadgen.yaml   # run the load
kubectl --context kind-rpcstack-tracebench -n rpcstack-tracebench logs job/tb-loadgen
scripts/k8s-teardown.sh --cluster           # remove everything
```

`KIND_CLUSTER`, `REGISTRY_NAME` and `REGISTRY_PORT` override the cluster name,
registry container and its host port. `k8s-loadgen.yaml` references the
baseline-tagged image; use
`MODE=masa scripts/lib/render-k8s.sh deploy/default/k8s-loadgen.yaml | kubectl apply -f -`
to run it against a masa deployment.

### Choosing or preparing a graph

A graph config is a small JSON file (`config/default.json`):

```json
{
  "callgraph_dirs": ["trace-analysis/graph_branch/S_14677443"],
  "slo_ms": 100,
  "slo_ms_by_graph": {},
  "replicas": { "default": 1, "overrides": { "MS_56394": 2 } },
  "loadgen": { "rps": 200, "duration_secs": 30, "warmup_secs": 0,
               "max_in_flight": 100, "stats_interval_secs": 2 }
}
```

`callgraph_dirs` are repository-relative graph directories (several graphs
share services; each gets its own root and a request share of `1/N`). Pick one
of the prepared directories under `trace-analysis/golden/`,
`trace-analysis/graph_branch/` or `trace-analysis/graphs/`, or prepare new ones
from the public Alibaba traces with the tooling in
[trace-analysis](trace-analysis/README.md) (graph directory contract: `edges.csv`,
`interface_distribution.json`, `latency_percentiles.json`, optional
`call_sequence.json`). Graph directories are copied into the image at
`/app/trace-analysis`, so rebuild after adding one. Then generate and run:

```bash
uv run --project tools tracebench-deploy config/mygraph.json --out deploy/mygraph
docker compose -f deploy/mygraph/docker-compose.yaml up --build
DEPLOY_DIR=deploy/mygraph scripts/k8s-deploy.sh masa      # Kubernetes
```

`scripts/gen-all.sh` regenerates the committed default
(`docker-compose.yaml`, `deploy/default/`); a test fails if they go stale.
Large graphs (for example `S_86516878`) need many more cores and memory.

### Smoke tests

`scripts/smoke-compose.sh [baseline|masa]` and `scripts/smoke-k8s.sh [baseline|masa]`
build and deploy the default graph, then run: a readiness wait; a control
phase of real Root requests through the whole graph; the policy-liveness probe
(an expired-deadline request to every service must be answered with Masa's
`/EarlyReturn` in masa mode and never in baseline, so it fails if the policy is
not wired in); moderate load with zero transport errors and an ok rate of at
least 0.99; in masa mode an overload burst that must be shed and a recovery
run; finally no panics and no restarts. In baseline mode the smoke also runs
the probe with `--expect masa` as a negative control that must fail with
`PROBE FAIL`, so a probe that cannot tell the modes apart breaks the smoke.

### Development

```bash
scripts/format.sh          # rustfmt
scripts/check.sh           # fmt, rpcstack pin, single tokio/hyper, warning-free build in both modes
scripts/test.sh            # cargo tests in both modes (incl. the hooks-path regression test)
(cd tools && uv run --group dev pytest)
```

Every `tonic_build` call must set `.default_hooks_path("masa::DefaultHooks")`
(`crates/generic-service/build.rs`); `crates/generic-service/tests/hooks.rs`
fails otherwise. Cargo builds are expected on Linux (Docker or a Linux host).

### Provenance

Ported from `vic-lsh/masa` `apps/tracebench` at commit `c8486eb` (generic
service, trace-config, loadgen, deployment tooling), built on rpcstack
`de0cedc080882469b316c8125b7ad17a7c93e7fb`. The experiment runner, evaluation
configs and policy sweeps were not ported. The old Python orchestrator was
reduced to what deployment generation needs (service discovery, root targets,
Compose and Kubernetes output).

## Trace tooling

`trace-analysis/` turns raw Alibaba cluster-trace-microservices-v2022 CallGraph
shards into the graph directories (`edges.csv`, `interface_distribution.json`,
`latency_percentiles.json`, optional `call_sequence.json`) that tracebench
consumes, and ships a set of prepared directories (`golden/`, `graph_branch/`,
`graphs/`).

```bash
cd trace-analysis
uv sync --group dev
uv run pytest                                   # offline tests
./fetch_traces.sh 0 1                           # ~220 MB download, ~1.4 GB CSV, into ../traces/
uv run python conditional_variants.py --num-datasets 1 --top-services 5 \
    --sample-trace-frac 0.01 --service-dir-layout --output-dir graph_branch
```

See [trace-analysis/README.md](trace-analysis/README.md) for the full flow, the
graph directory contract, and the inventory of prepared data.
