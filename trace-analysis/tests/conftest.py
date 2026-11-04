"""Synthetic Alibaba CallGraph rows (v2022 schema) for offline pipeline tests."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

HEADER = [
    "timestamp", "traceid", "service", "rpc_id", "rpctype",
    "um", "uminstanceid", "interface", "dm", "dminstanceid", "rt",
]


def _row(ts, trace, svc, rpc_id, um, iface, dm, rt, rpctype="rpc"):
    return [ts, trace, svc, rpc_id, rpctype, um, f"{um}_POD_0", iface, dm,
            f"{dm}_POD_0", rt]


def synthetic_rows() -> list[list]:
    """S_1: USER->A, A fans out to B and C in parallel, then calls D after;
    in odd traces C also calls D (a second variant of C). S_2: USER->X->B.
    Also includes rows the pipeline must filter (mq, UNKNOWN)."""
    rows = []
    for t in range(40):
        tr = f"T_{t}"
        rows.append(_row(0, tr, "S_1", "0.1", "USER", "ia", "MS_A", 50 + t))
        rows.append(_row(10, tr, "S_1", "0.1.1", "MS_A", "ib", "MS_B", 5 + t % 3))
        rows.append(_row(10, tr, "S_1", "0.1.2", "MS_A", "ic", "MS_C", 8 + t % 5))
        if t % 2:
            rows.append(_row(12, tr, "S_1", "0.1.2.1", "MS_C", "id", "MS_D", 3))
        rows.append(_row(30, tr, "S_1", "0.1.3", "MS_A", "id", "MS_D", 4))
        rows.append(_row(35, tr, "S_1", "0.1.4", "MS_A", "ix", "MS_Q", 1, rpctype="mq"))
    for t in range(15):
        tr = f"T_x{t}"
        rows.append(_row(0, tr, "S_2", "0.1", "USER", "ix", "MS_X", 20 + t))
        rows.append(_row(5, tr, "S_2", "0.1.1", "MS_X", "ib", "MS_B", 6 + t % 4))
        rows.append(_row(5, tr, "S_2", "0.1.2", "UNKNOWN", "iu", "MS_B", 6))
    return rows


@pytest.fixture(scope="session")
def trace_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("traces") / "CallGraph"
    d.mkdir()
    with open(d / "CallGraph_0.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(synthetic_rows())
    return d
