"""Generate the tracebench deployment for one graph configuration.

Input is a small JSON config (see ``config/default.json``): which prepared graph
directories to instantiate, the SLO, replica counts and load-generator settings.
Output, all deterministic (sorted, no timestamps):

* ``deployment.json``  service discovery for the generic services
* ``frontend.json``    root-service targets for the load generator
* ``docker-compose.yaml``  one compose service per replica plus the load generator
* ``k8s.yaml``         namespace, ConfigMap, one Deployment + Service per replica
* ``k8s-loadgen.yaml`` the load generator as a Kubernetes Job

Naming is fixed by the application: ``LoadBalancedChannel`` dials
``<ip>-<n>:<port>`` for n in 1..=replicas, so ``deployment.json`` stores
``ip = tb-<service>`` and every replica is a workload named ``tb-<service>-<n>``
in both Compose and Kubernetes.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .deployment import Deployment, ServiceDiscoveryInfo
from .replica_config import ReplicaConfig
from .trace_config import TraceConfig, TraceConfigError
from .utils import normalize_service_name
from .validator import validate_config
from .yaml_utils import dump_yaml

PREFIX = "tb"
PORT = 50051
IMAGE = "rpcstack-tracebench"
DEFAULT_MODE = "baseline"
DEFAULT_REGISTRY = "localhost:5001"
NAMESPACE = "rpcstack-tracebench"
FRONTEND = normalize_service_name("USER")
# Where generated config files are mounted inside the containers.
CONF_DIR = "/etc/tracebench"
CPU_LIMIT = "1"
LOADGEN_DEFAULTS = {
    "rps": 200,
    "duration_secs": 30,
    "warmup_secs": 0,
    "max_in_flight": 100,
    "stats_interval_secs": 2,
}


class ConfigError(RuntimeError):
    """Raised when the graph configuration is invalid."""


@dataclass
class GraphConfig:
    callgraph_dirs: list[str]
    slo_ms: int = 100
    slo_ms_by_graph: dict[str, int] = field(default_factory=dict)
    replicas: ReplicaConfig = field(default_factory=ReplicaConfig)
    loadgen: dict[str, Any] = field(default_factory=lambda: dict(LOADGEN_DEFAULTS))

    @classmethod
    def from_file(cls, path: Path) -> "GraphConfig":
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        dirs = raw.get("callgraph_dirs")
        if not dirs or not isinstance(dirs, list):
            raise ConfigError(f"{path}: 'callgraph_dirs' must be a non-empty list")
        for d in dirs:
            p = PurePosixPath(d)
            if p.is_absolute() or ".." in p.parts:
                raise ConfigError(
                    f"{path}: callgraph dir {d!r} must be a relative path inside the repository "
                    "(it is baked into the image at /app/<path>)"
                )
        if len({PurePosixPath(d).name for d in dirs}) != len(dirs):
            raise ConfigError(f"{path}: callgraph directory names must be unique")
        loadgen = dict(LOADGEN_DEFAULTS)
        loadgen.update(raw.get("loadgen", {}))
        return cls(
            callgraph_dirs=[str(PurePosixPath(d)) for d in dirs],
            slo_ms=int(raw.get("slo_ms", 100)),
            slo_ms_by_graph={str(k): int(v) for k, v in raw.get("slo_ms_by_graph", {}).items()},
            replicas=ReplicaConfig.from_dict(raw.get("replicas")),
            loadgen=loadgen,
        )

    def graph_name(self, directory: str) -> str:
        """Graph id used by Root requests, e.g. ``S_14677443`` -> ``s-14677443``."""
        return normalize_service_name(PurePosixPath(directory).name)


def host(service: str, n: int) -> str:
    return f"{PREFIX}-{service}-{n}"


def frontend_name(graph: str) -> str:
    return f"{FRONTEND}-{graph}"


@dataclass
class Plan:
    config: GraphConfig
    deployment: Deployment
    frontends: list[dict[str, Any]]
    # service name -> (SERVICE_NAME env, replicas), sorted by name
    workloads: list[tuple[str, str, int]]


def build_plan(config: GraphConfig, root: Path) -> Plan:
    trace_configs = []
    for d in config.callgraph_dirs:
        directory = root / d
        if not directory.is_dir():
            raise ConfigError(f"call graph directory does not exist: {directory}")
        try:
            tc = TraceConfig.from_config_dir(directory)
        except TraceConfigError as exc:
            raise ConfigError(str(exc)) from exc
        validate_config(tc)
        trace_configs.append(tc)

    backends = sorted(TraceConfig.union_services(trace_configs) - {FRONTEND})
    graphs = [config.graph_name(d) for d in config.callgraph_dirs]

    deployment = Deployment()
    workloads: list[tuple[str, str, int]] = []
    # One root ("user") service per graph, then every other service once.
    for g in graphs:
        name = frontend_name(g)
        n = config.replicas.count_for(FRONTEND)
        deployment.add_service(name, ServiceDiscoveryInfo(ip=f"{PREFIX}-{name}", port=PORT, replicas=n))
        workloads.append((name, "USER", n))
    for name in backends:
        n = config.replicas.count_for(name)
        deployment.add_service(name, ServiceDiscoveryInfo(ip=f"{PREFIX}-{name}", port=PORT, replicas=n))
        workloads.append((name, name, n))

    frontends = []
    for d, g in zip(config.callgraph_dirs, graphs):
        info = deployment.services[frontend_name(g)]
        frontends.append(
            {
                "service": frontend_name(g),
                "ip": info.ip,
                "port": info.port,
                "replicas": info.replicas,
                "graph": g,
                "slo_ms": config.slo_ms_by_graph.get(PurePosixPath(d).name, config.slo_ms),
                "probability": 1.0 / len(graphs),
            }
        )
    return Plan(config, deployment, frontends, workloads)


def deployment_json(plan: Plan) -> str:
    return json.dumps({"services": plan.deployment.to_dict()}, indent=2) + "\n"


def frontend_json(plan: Plan) -> str:
    return json.dumps(plan.frontends, indent=2) + "\n"


def service_env(plan: Plan, env_name: str) -> dict[str, str]:
    return {
        "LOG_LEVEL": "${LOG_LEVEL:-info}",
        "SERVICE_NAME": env_name,
        "SERVICE_PORT": str(PORT),
        "DEPLOYMENT_CONFIG_PATH": f"{CONF_DIR}/deployment.json",
        "CALLGRAPH_DIRS": ",".join(plan.config.callgraph_dirs),
    }


def loadgen_env(plan: Plan) -> dict[str, str]:
    lg = plan.config.loadgen
    return {
        "RPS": str(lg["rps"]),
        "DURATION": str(lg["duration_secs"]),
        "WARMUP_SEC": str(lg["warmup_secs"]),
        "MAX_IN_FLIGHT": str(lg["max_in_flight"]),
        "STATS_INTERVAL_SEC": str(lg["stats_interval_secs"]),
        "FRONTEND_CONFIG": f"{CONF_DIR}/frontend.json",
        "WAIT_FRONTENDS": wait_frontends(plan),
    }


def wait_frontends(plan: Plan) -> str:
    return ",".join(f"{f['graph']}=http://{host(f['service'], 1)}:{f['port']}" for f in plan.frontends)


# ------------------------------------------------------------------- Compose

def _dot(rel: str) -> str:
    return rel if rel.startswith(".") else f"./{rel}"


def compose_yaml(plan: Plan, deploy_rel: str, context_rel: str = ".") -> str:
    """Compose project. Paths are relative to the compose file's directory."""
    image = f"{IMAGE}:${{MODE:-{DEFAULT_MODE}}}"
    common = {
        "image": image,
        "pull_policy": "never",
        "restart": "unless-stopped",
        "volumes": [f"{_dot(deploy_rel)}:{CONF_DIR}:ro"],
    }
    limits = {"deploy": {"resources": {"limits": {"cpus": CPU_LIMIT}}}}
    build = {
        "context": context_rel,
        "args": {
            "MODE": f"${{MODE:-{DEFAULT_MODE}}}",
            "FEATURES": "${FEATURES:-}",
            "BUILD_JOBS": "${BUILD_JOBS:-}",
        },
    }
    services: dict[str, Any] = {}
    first = True
    for name, env_name, replicas in plan.workloads:
        for n in range(1, replicas + 1):
            svc: dict[str, Any] = {
                **common,
                "environment": service_env(plan, env_name),
                "command": ["service"],
                **limits,
            }
            if first:
                # One service carries the build; all others reuse its image.
                svc["build"] = build
                first = False
            services[host(name, n)] = svc
    services["loadgen"] = {
        **common,
        "restart": "no",
        "environment": {"LOG_LEVEL": "${LOG_LEVEL:-info}", **loadgen_env(plan)},
        "command": ["loadgen"],
        "depends_on": {
            host(w[0], 1): {"condition": "service_started"} for w in plan.workloads if w[1] == "USER"
        },
    }
    header = (
        "# Generated by tools/ (tracebench-deploy). Do not edit; regenerate with scripts/gen-all.sh.\n"
        "#\n"
        "#   docker compose up --build                 # baseline (no policy)\n"
        "#   MODE=masa docker compose up --build       # Masa composed policy\n"
        "#\n"
        "# Every process runs the same locally built image; the first container\n"
        "# argument selects the binary (docker/entrypoint.sh).\n"
    )
    return header + dump_yaml({"services": services})


# ---------------------------------------------------------------- Kubernetes

def k8s_labels(name: str) -> dict[str, str]:
    return {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": "tracebench"}


def _configmap(plan: Plan, with_deployment: bool) -> dict[str, Any]:
    data = {"frontend.json": frontend_json(plan)}
    if with_deployment:
        data = {"deployment.json": deployment_json(plan), **data}
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "tb-config", "namespace": NAMESPACE, "labels": {"app.kubernetes.io/part-of": "tracebench"}},
        "data": data,
    }


def _env_list(env: dict[str, str]) -> list[dict[str, str]]:
    out = []
    for k, v in env.items():
        if v == "${LOG_LEVEL:-info}":
            v = "info"
        out.append({"name": k, "value": v})
    return out


def _config_volume() -> tuple[list[dict], list[dict]]:
    return (
        [{"name": "config", "mountPath": CONF_DIR, "readOnly": True}],
        [{"name": "config", "configMap": {"name": "tb-config"}}],
    )


def k8s_yaml(plan: Plan, registry: str = DEFAULT_REGISTRY) -> str:
    image = f"{registry}/{IMAGE}:{DEFAULT_MODE}"
    mounts, volumes = _config_volume()
    docs: list[dict[str, Any]] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
        _configmap(plan, with_deployment=True),
    ]
    for name, env_name, replicas in plan.workloads:
        for n in range(1, replicas + 1):
            wl = host(name, n)
            labels = {**k8s_labels(wl), "app.kubernetes.io/component": "service"}
            docs.append(
                {
                    "apiVersion": "apps/v1",
                    "kind": "Deployment",
                    "metadata": {"name": wl, "namespace": NAMESPACE, "labels": labels},
                    "spec": {
                        "replicas": 1,
                        "selector": {"matchLabels": {"app.kubernetes.io/name": wl}},
                        "template": {
                            "metadata": {"labels": labels},
                            "spec": {
                                "containers": [
                                    {
                                        "name": "service",
                                        "image": image,
                                        "imagePullPolicy": "IfNotPresent",
                                        "args": ["service"],
                                        "env": _env_list(service_env(plan, env_name)),
                                        "ports": [{"name": "grpc", "containerPort": PORT}],
                                        "readinessProbe": {"tcpSocket": {"port": PORT}, "periodSeconds": 3},
                                        "resources": {
                                            "limits": {"cpu": CPU_LIMIT},
                                            "requests": {"cpu": "100m"},
                                        },
                                        "volumeMounts": mounts,
                                    }
                                ],
                                "volumes": volumes,
                            },
                        },
                    },
                }
            )
            docs.append(
                {
                    "apiVersion": "v1",
                    "kind": "Service",
                    "metadata": {"name": wl, "namespace": NAMESPACE, "labels": labels},
                    "spec": {
                        "selector": {"app.kubernetes.io/name": wl},
                        "ports": [{"name": "grpc", "port": PORT, "targetPort": PORT}],
                    },
                }
            )
    header = (
        "# Generated by tools/ (tracebench-deploy). Do not edit; regenerate with scripts/gen-all.sh.\n"
        f"# Image {image} is built and pushed by scripts/k8s-deploy.sh, which rewrites\n"
        "# the registry and mode tag.\n"
    )
    return header + "---\n".join(_one_doc(d) for d in docs)


def _one_doc(doc: dict[str, Any]) -> str:
    return "".join(dump_yaml(doc))


def k8s_loadgen_yaml(plan: Plan, registry: str = DEFAULT_REGISTRY) -> str:
    """The load generator as a Job. Environment overrides (RPS, DURATION, ...) are
    applied by scripts/smoke-k8s.sh."""
    image = f"{registry}/{IMAGE}:{DEFAULT_MODE}"
    mounts, volumes = _config_volume()
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "tb-loadgen", "namespace": NAMESPACE, "labels": k8s_labels("tb-loadgen")},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": k8s_labels("tb-loadgen")},
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "loadgen",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": ["loadgen"],
                            "env": _env_list({"LOG_LEVEL": "${LOG_LEVEL:-info}", **loadgen_env(plan)}),
                            "volumeMounts": mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }
    header = (
        "# Generated by tools/ (tracebench-deploy). Do not edit; regenerate with scripts/gen-all.sh.\n"
        "# Load generator Job; apply after k8s.yaml: kubectl apply -f k8s-loadgen.yaml\n"
    )
    return header + _one_doc(job)


# -------------------------------------------------------------------- output

def generate(
    config_path: Path,
    out_dir: Path,
    root: Path,
    compose_out: Path | None = None,
) -> list[Path]:
    """Write all generated files and return their paths."""
    config = GraphConfig.from_file(config_path)
    plan = build_plan(config, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        out_dir / "deployment.json": deployment_json(plan),
        out_dir / "frontend.json": frontend_json(plan),
        out_dir / "k8s.yaml": k8s_yaml(plan),
        out_dir / "k8s-loadgen.yaml": k8s_loadgen_yaml(plan),
    }
    compose_path = compose_out or (out_dir / "docker-compose.yaml")
    compose_dir = compose_path.parent
    files[compose_path] = compose_yaml(
        plan,
        deploy_rel=os.path.relpath(out_dir.resolve(), compose_dir.resolve()),
        context_rel=os.path.relpath(root.resolve(), compose_dir.resolve()),
    )
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return sorted(files)
