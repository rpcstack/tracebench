import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "fetch_traces.sh"


def run(*args, **env):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env={**os.environ, **env})


def test_dry_run_prints_upstream_urls_without_downloading():
    r = run("2", "4", DRY_RUN="1")
    assert r.returncode == 0
    assert ("https://aliopentrace.oss-cn-beijing.aliyuncs.com/"
            "v2022MicroservicesTraces/CallGraph/CallGraph_2.tar.gz") in r.stdout
    assert "CallGraph_3.tar.gz" in r.stdout and "CallGraph_4.tar.gz" not in r.stdout


def test_rejects_bad_range():
    assert run("4", "2").returncode != 0
    assert run().returncode != 0
