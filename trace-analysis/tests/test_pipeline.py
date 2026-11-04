"""Run the real conditional_variants pipeline on a synthetic CSV and check the
output directory against what the tracebench Rust consumer (`trace-config`)
reads:

* edges.csv: header with `caller`,`callee`, optional integer `weight` >= 0
* interface_distribution.json: { service: { method: count } }
* latency_percentiles.json: { service: { method: { percentile: latency } } }
* call_sequence.json: { graph: { format: "conditional_variants",
  methods: { method: { variants: { id: { probability,
  sequence: [[ { target: "svc::method", callee_variants?: {id: p} } ]] } } } } } }
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "conditional_variants.py"


def run_pipeline(trace_dir: Path, out: Path) -> None:
    subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--trace-dir", str(trace_dir),
         "--dataset-ids", "0", "--service", "S_1", "--service", "S_2",
         "--service-dir-layout", "--output-dir", str(out)],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture(scope="module")
def out_dir(trace_dir, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("graphs")
    run_pipeline(trace_dir, out)
    return out


def load(p: Path):
    return json.loads(p.read_text())


def test_graph_dirs_have_consumer_files(out_dir):
    for svc in ("S_1", "S_2"):
        d = out_dir / svc
        for name in ("edges.csv", "interface_distribution.json",
                     "latency_percentiles.json", "call_sequence.json"):
            assert (d / name).is_file(), f"{svc}/{name} missing"
        # Plots are not part of the consumer contract.
        assert not list(d.glob("*.png"))


def test_edges_csv_matches_consumer_schema(out_dir):
    with open(out_dir / "S_1" / "edges.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {"caller", "callee", "weight"} <= set(rows[0])
    edges = {(r["caller"], r["callee"]): r["weight"] for r in rows}
    # Rust parses weight as a non-negative integer-valued number.
    assert all(float(w) >= 0 and float(w) == int(float(w)) for w in edges.values())
    assert edges[("USER", "MS_A")] == "40"
    assert edges[("MS_A", "MS_B")] == "40"
    assert edges[("MS_A", "MS_C")] == "40"
    assert edges[("MS_C", "MS_D")] == "20"
    assert edges[("MS_A", "MS_D")] == "40"
    # mq rows are filtered out.
    assert not any("MS_Q" in k for k in edges)


def test_interface_distribution_schema(out_dir):
    dist = load(out_dir / "S_1" / "interface_distribution.json")
    for svc, methods in dist.items():
        assert isinstance(svc, str)
        for method, count in methods.items():
            assert isinstance(method, str) and isinstance(count, int)
    assert dist["MS_A"] == {"ia": 40}
    assert dist["MS_D"] == {"id": 60}


def test_latency_percentiles_schema(out_dir):
    lat = load(out_dir / "S_1" / "latency_percentiles.json")
    assert set(lat) == {"MS_A", "MS_B", "MS_C", "MS_D"}
    for methods in lat.values():
        for pmap in methods.values():
            assert len(pmap) >= 100
            vals = []
            for k, v in sorted(pmap.items(), key=lambda kv: float(kv[0])):
                float(k)  # percentile keys parse as numbers
                assert isinstance(v, float)
                vals.append(v)
            assert vals == sorted(vals)  # monotone in percentile
    assert lat["MS_D"]["id"]["100"] == 4.0


def test_call_sequence_schema_and_variants(out_dir):
    cs = load(out_dir / "S_1" / "call_sequence.json")
    assert list(cs) == ["S_1"]
    g = cs["S_1"]
    assert g["format"] == "conditional_variants"
    methods = g["methods"]
    for name, m in methods.items():
        for vid, v in m["variants"].items():
            assert 0.0 <= v["probability"] <= 1.0
            for step in v["sequence"]:
                for call in step:
                    parts = call["target"].split("::")
                    assert len(parts) == 2 and all(parts)
                    for p in call.get("callee_variants", {}).values():
                        assert 0.0 <= p <= 1.0
        total = sum(v["probability"] for v in m["variants"].values())
        assert total == pytest.approx(1.0)
    # A fans out to B and C in parallel, then D: two stages.
    (variant,) = methods["MS_A::ia"]["variants"].values()
    assert [sorted(c["target"] for c in step) for step in variant["sequence"]] == [
        ["MS_B::ib", "MS_C::ic"], ["MS_D::id"]]
    # C has two variants (with / without the D call), 50% each.
    probs = sorted(v["probability"] for v in methods["MS_C::ic"]["variants"].values())
    assert probs == pytest.approx([0.5, 0.5])


def test_summary_csv_written(out_dir):
    with open(out_dir / "summary.csv", newline="") as f:
        rows = {r["service"]: r for r in csv.DictReader(f)}
    assert rows["S_1"]["trace_count"] == "40"
    assert rows["S_2"]["trace_count"] == "15"


def test_pipeline_is_deterministic(trace_dir, out_dir, tmp_path):
    second = tmp_path / "again"
    run_pipeline(trace_dir, second)
    for svc in ("S_1", "S_2"):
        for name in ("edges.csv", "interface_distribution.json",
                     "latency_percentiles.json", "call_sequence.json"):
            assert (out_dir / svc / name).read_bytes() == (second / svc / name).read_bytes(), name
