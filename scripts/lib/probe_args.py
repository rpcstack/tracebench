#!/usr/bin/env python3
"""Print `tracebench-probe` target arguments for a generated deployment.json.

    probe_args.py deploy/default/deployment.json [--wait-frontends]

Root services (`user-<graph>`) become `--frontend <graph>=http://<host>:<port>`,
every other service `--target <name>=http://<host>:<port>`. The first replica of
each service is probed; hostnames are `<ip>-1`, as dialled by LoadBalancedChannel.
Kubernetes Services carry the same names, so the output works there too.
"""
import json
import sys

with open(sys.argv[1]) as f:
    services = json.load(f)["services"]
if "--wait-frontends" in sys.argv:
    # WAIT_FRONTENDS value for the load generator entrypoint: graph=url,...
    print(",".join(
        f"{n[len('user-'):]}=http://{i['ip']}-1:{i['port']}"
        for n, i in sorted(services.items()) if n.startswith("user-")
    ))
    sys.exit(0)
args = []
for name, info in sorted(services.items()):
    url = f"http://{info['ip']}-1:{info['port']}"
    if name.startswith("user-"):
        args += ["--frontend", f"{name[len('user-'):]}={url}"]
    else:
        args += ["--target", f"{name}={url}"]
print(" ".join(args))
