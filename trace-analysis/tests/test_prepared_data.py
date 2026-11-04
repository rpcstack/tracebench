"""Committed graph directories must stay readable by the tracebench consumer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GRAPH_DIRS = sorted(
    p.parent for sub in ("golden", "graph_branch", "graphs") for p in (ROOT / sub).glob("*/edges.csv")
)


def test_graph_dirs_present():
    assert len(GRAPH_DIRS) >= 8


@pytest.mark.parametrize("d", GRAPH_DIRS, ids=lambda d: f"{d.parent.name}/{d.name}")
def test_graph_dir_consumable(d):
    with open(d / "edges.csv", newline="") as f:
        reader = csv.DictReader(f)
        assert {"caller", "callee"} <= set(reader.fieldnames)
        for row in reader:
            w = row.get("weight")
            if w not in (None, ""):
                assert float(w) >= 0 and float(w) == round(float(w))
    assert not list(d.glob("*.png")), "plots must not be committed"
    if (d / "interface_distribution.json").exists():
        dist = json.loads((d / "interface_distribution.json").read_text())
        assert all(isinstance(c, (int, float)) for m in dist.values() for c in m.values())
    if (d / "latency_percentiles.json").exists():
        lat = json.loads((d / "latency_percentiles.json").read_text())
        for methods in lat.values():
            for pmap in methods.values():
                assert all(float(k) >= 0 and isinstance(v, (int, float)) for k, v in pmap.items())
    if (d / "call_sequence.json").exists():
        cs = json.loads((d / "call_sequence.json").read_text())
        assert isinstance(cs, dict) and cs
        for graph in cs.values():
            if graph.get("format") == "conditional_variants":
                assert all("variants" in m for m in graph["methods"].values())
