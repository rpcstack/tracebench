from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .generate import ConfigError, generate
from .replica_config import ReplayConfigError
from .trace_config import TraceConfigError
from .validator import ValidationError


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tracebench-deploy",
        description="Generate Docker Compose and Kubernetes files for a tracebench graph config.",
    )
    parser.add_argument("config", type=Path, help="graph config JSON, e.g. config/default.json")
    parser.add_argument("--out", type=Path, required=True, help="directory for deployment.json, frontend.json, k8s*.yaml")
    parser.add_argument(
        "--compose-out",
        type=Path,
        default=None,
        help="where to write docker-compose.yaml (default: <out>/docker-compose.yaml)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repository root that callgraph_dirs are relative to (default: .)",
    )
    args = parser.parse_args(argv)
    try:
        paths = generate(args.config, args.out, args.root, args.compose_out)
    except (ConfigError, TraceConfigError, ReplayConfigError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p in paths:
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
