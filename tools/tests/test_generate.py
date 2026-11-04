import json
import shutil
from pathlib import Path

import pytest

from tracebench_deploy.cli import main
from tracebench_deploy.generate import ConfigError, GraphConfig, build_plan, generate
from tracebench_deploy.validator import ValidationError, validate_config
from tracebench_deploy.trace_config import CallGraph, TraceConfig

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "config" / "default.json"


def generate_default(out: Path) -> dict[str, str]:
    """Generate into a scratch repository laid out like the real one."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace-analysis").symlink_to(ROOT / "trace-analysis")
    paths = generate(DEFAULT, out / "deploy" / "default", out, out / "docker-compose.yaml")
    return {str(p.relative_to(out)): p.read_text() for p in paths}


def test_generation_is_deterministic(tmp_path):
    a = generate_default(tmp_path / "a")
    b = generate_default(tmp_path / "b")
    assert a == b
    assert sorted(a) == [
        "deploy/default/deployment.json",
        "deploy/default/frontend.json",
        "deploy/default/k8s-loadgen.yaml",
        "deploy/default/k8s.yaml",
        "docker-compose.yaml",
    ]


def test_committed_files_match_generator(tmp_path):
    """The committed default-graph deployment is exactly what the generator emits
    (run scripts/gen-all.sh after changing config/default.json or the generator)."""
    fresh = generate_default(tmp_path)
    committed = {
        "deploy/default/deployment.json": ROOT / "deploy/default/deployment.json",
        "deploy/default/frontend.json": ROOT / "deploy/default/frontend.json",
        "deploy/default/k8s-loadgen.yaml": ROOT / "deploy/default/k8s-loadgen.yaml",
        "deploy/default/k8s.yaml": ROOT / "deploy/default/k8s.yaml",
        "docker-compose.yaml": ROOT / "docker-compose.yaml",
    }
    for rel, path in committed.items():
        assert path.read_text() == fresh[rel], f"{path} is stale; run scripts/gen-all.sh"


def test_default_graph_services():
    plan = build_plan(GraphConfig.from_file(DEFAULT), ROOT)
    names = list(plan.deployment.services)
    assert names[0] == "user-s-14677443"
    assert names[1:] == sorted(names[1:])
    tc = TraceConfig.from_config_dir(ROOT / "trace-analysis/graph_branch/S_14677443")
    backends = {n for n in tc.call_graph.services() if n != "user"}
    assert set(names[1:]) == backends
    # LoadBalancedChannel dials <ip>-<n>:<port>; every replica needs a workload of that name.
    for name, env, replicas in plan.workloads:
        assert plan.deployment.services[name].ip == f"tb-{name}"
        assert plan.deployment.services[name].replicas == replicas
        assert env == ("USER" if name.startswith("user-") else name)


def test_replica_overrides_and_naming(tmp_path):
    cfg = json.loads(DEFAULT.read_text())
    cfg["replicas"] = {"default": 1, "overrides": {"MS_56394": 3}}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    paths = generate(path, tmp_path / "d", ROOT, tmp_path / "compose.yaml")
    compose = (tmp_path / "compose.yaml").read_text()
    for n in (1, 2, 3):
        assert f"  tb-ms-56394-{n}:\n" in compose
    assert "tb-ms-56394-4" not in compose
    dep = json.loads((tmp_path / "d/deployment.json").read_text())
    assert dep["services"]["ms-56394"]["replicas"] == 3
    # relative paths in the compose file point back to the output directory and context
    assert "../d:/etc/tracebench:ro" in compose or "d:/etc/tracebench:ro" in compose
    assert len(paths) == 5


def test_multi_graph_gets_one_frontend_per_graph(tmp_path):
    cfg = {
        "callgraph_dirs": [
            "trace-analysis/graph_branch/S_14677443",
            "trace-analysis/golden/S_32048416",
        ],
        "slo_ms_by_graph": {"S_32048416": 250},
    }
    path = tmp_path / "c.json"
    path.write_text(json.dumps(cfg))
    plan = build_plan(GraphConfig.from_file(path), ROOT)
    fronts = {f["graph"]: f for f in plan.frontends}
    assert set(fronts) == {"s-14677443", "s-32048416"}
    assert fronts["s-32048416"]["slo_ms"] == 250
    assert fronts["s-14677443"]["slo_ms"] == 100
    assert all(f["probability"] == 0.5 for f in plan.frontends)


@pytest.mark.parametrize(
    "dirs",
    [["/etc/passwd"], ["../outside"], [], ["a/S_1", "b/S_1"]],
)
def test_bad_graph_dirs_are_rejected(tmp_path, dirs):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"callgraph_dirs": dirs}))
    with pytest.raises(ConfigError):
        GraphConfig.from_file(path)


def test_missing_graph_dir_is_reported(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"callgraph_dirs": ["trace-analysis/nope/S_1"]}))
    assert main([str(path), "--out", str(tmp_path / "o"), "--root", str(ROOT)]) == 1


def test_validator_rejects_empty_graph():
    with pytest.raises(ValidationError):
        validate_config(TraceConfig(call_graph=CallGraph()))


def test_cli_end_to_end(tmp_path):
    rc = main([str(DEFAULT), "--out", str(tmp_path / "o"), "--root", str(ROOT)])
    assert rc == 0
    assert (tmp_path / "o/docker-compose.yaml").exists()
