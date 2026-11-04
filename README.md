# tracebench

A microservice benchmark that builds a service graph from prepared Alibaba
call-graph directories.

## Application

_Placeholder: the Rust tracebench application, its images and deployment will be
documented here._

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
