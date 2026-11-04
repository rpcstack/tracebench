# trace-analysis

Tooling that turns the public Alibaba `cluster-trace-microservices-v2022`
CallGraph traces into the **graph directories** that tracebench builds its
service graph from, plus the prepared graph directories checked in here.

Run everything from this directory with [uv](https://docs.astral.sh/uv/):

```bash
cd trace-analysis
uv sync                 # numpy, pandas, networkx, matplotlib, tqdm
uv sync --extra plots   # + pygraphviz (needs system graphviz) for the plotting scripts
uv sync --group dev && uv run pytest   # offline regression tests, a few seconds
```

## End-to-end: raw CallGraph shards to a graph directory

1. **Fetch and decompress shards** into the gitignored `<repo>/traces/CallGraph/`
   (upstream public OSS bucket, see "Where the data comes from"). Each shard is
   about 220 MB compressed and 1.4 GB as CSV, so fetch only what you need:

   ```bash
   ./fetch_traces.sh 0 2            # shards 0 and 1 (half-open range)
   ./fetch_traces.sh --only 7       # a single shard
   KEEP_ARCHIVES=1 ./fetch_traces.sh 0 2   # keep the .tar.gz files
   DRY_RUN=1 ./fetch_traces.sh 0 25        # print the URLs only
   ```

   Already-downloaded archives can be unpacked with
   `./decompress_call_graph.sh I J` (shards I..J-1, from `traces/CallGraph/`).

2. **Build graph directories** with `conditional_variants.py`. This is the
   pipeline that produces what the tracebench Rust consumer reads today
   (`call_sequence.json` in `conditional_variants` format):

   ```bash
   uv run python conditional_variants.py \
       --num-datasets 2 --top-services 5 \
       --sample-trace-frac 0.01 --workers 8 \
       --service-dir-layout --output-dir graph_branch
   # or specific services / shards:
   uv run python conditional_variants.py --dataset-ids 0,2,4-6 \
       --service S_14677443 --service S_32048416 \
       --service-dir-layout --output-dir graph_branch
   ```

   Without `--service-dir-layout` only the aggregate `conditional_variants.json`
   and `summary.csv` are written. The checked-in `graph_branch/` was produced
   from shards 0-24 with `--sample-trace-frac 0.01` (hash sampling, seed 42,
   overlap threshold 0.1), see the `metadata` block in each `call_sequence.json`.

3. **Point tracebench at it**: each service directory
   `graph_branch/S_<id>/` is one entry of `callgraph_dirs` in a tracebench graph
   config (`config/default.json` in the repository root, see the
   [top-level README](../README.md#choosing-or-preparing-a-graph)). Rebuild the
   image afterwards, graph directories are copied into it.

### Graph directory contract

| File | Required | Content |
|---|---|---|
| `edges.csv` | yes | header with `caller`, `callee`; optional integer `weight` (>= 0, defaults to 1); extra columns such as `service` are ignored |
| `interface_distribution.json` | yes (optional in the parser, needed for method sampling) | `{ service: { method: count } }` |
| `latency_percentiles.json` | yes for latency replay | `{ service: { method: { percentile: latency_ms } } }` |
| `call_sequence.json` | optional | `{ graph_id: { "format": "conditional_variants", "methods": { "svc::method": { "variants": { id: { "probability", "sequence": [[ { "target": "svc::method", "callee_variants": { id: p } } ]] } } } } } }`; a legacy marginal-probability layout is still accepted by the consumer |

The graph name used by tracebench is the directory name (for example `S_14677443`).
`tests/test_pipeline.py` runs the real pipeline on a synthetic CSV and asserts
this contract; `tests/test_prepared_data.py` checks every committed directory.

## Scripts

| Script | What it does |
|---|---|
| `fetch_traces.sh` | Download + decompress CallGraph shards from the public upstream bucket. |
| `decompress_call_graph.sh I J` | Extract already-downloaded `CallGraph_<i>.tar.gz` for i in [I, J). |
| `conditional_variants.py` | Reconstructs traces from `rpc_id` parentage and span timing, learns conditional fanout variants, and (with `--service-dir-layout`) writes full graph directories without plots. |
| `analyze.py -n N` | Older path: loads the first N shards, filters UNKNOWN/mq, and writes per-service `edges.csv`, `interface_distribution.json`, `latency_percentiles.json` plus PNG plots to `graph_reports/`. Needs pygraphviz; worker count is hard-coded to 32. |
| `quickcheck.py` | Older path, a large script that samples traces and writes plot-rich per-service directories (with legacy-format `call_sequence.json`) to `graphs/`. Needs pygraphviz. |
| `cycle_check.py edges.csv` | Detects whether an `edges.csv` call graph has a cycle (exit 1 if so). |
| `find_shared_callees.py` | Lists `MS_*` callees shared across `graph_reports/S_*/edges.csv`. |
| `find_shared_nodes.py` | Ranks service pairs sharing nodes in `graphs/S_*/edges.csv` and draws overlap plots. Needs pygraphviz. |

Except for default paths (traces live in `traces/CallGraph/`) the scripts are
unchanged from the research repo they were ported from.

## Where the data comes from

Public upstream: `https://aliopentrace.oss-cn-beijing.aliyuncs.com/v2022MicroservicesTraces/CallGraph/CallGraph_<i>.tar.gz`
(the URL used by `cluster-trace-microservices-v2022/fetchData.sh` in
[alibaba/clusterdata](https://github.com/alibaba/clusterdata/tree/master/cluster-trace-microservices-v2022)).
The research repo used a fork (`vic-lsh/alibaba-clusterdata`) as a submodule;
at the time of porting that fork has no commits beyond upstream (0 ahead, 24
behind) and contains no trace data, so nothing is lost by using upstream.

## Prepared data

Checked in so tracebench runs without downloading 5+ GB of traces. Only files the
consumer reads are kept (no plots). The `trace_dir` field in each
`call_sequence.json` metadata was rewritten to `traces/CallGraph` (it recorded a
machine-local absolute path); nothing else was modified.

| Directory | Used for |
|---|---|
| `golden/S_86516878`, `golden/S_14677443`, `golden/S_32048416`, `golden/hotel` | the "golden" examples (hotel is a hand-written demo graph) |
| `graph_branch/S_14677443`, `S_32048416`, `S_86516878`, `S_98493745`, `S_130831269`, `S_5991695` | `callgraph_dirs` of the research repo's experiment configs |
| `graph_branch/summary.csv` | per-graph trace/span/method/variant counts for all 25 graphs |
| `graphs/S_38528029` | referenced by one experiment config |

The other 19 `graph_branch/` graphs and the aggregate `conditional_variants.json`
are not checked in; regenerate them with step 2 above.
