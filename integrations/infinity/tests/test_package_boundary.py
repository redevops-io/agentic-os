"""The installed package loads no cloud SDK and no Infinity module of its own (the Rust runtime is a separate program)."""
import json
import subprocess
import sys


def test_importing_the_package_loads_no_cloud_sdk():
    code = "import sys, json, redevops_infinity; print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert not (loaded & {"boto3", "botocore", "aws_cdk", "anthropic", "openai", "infinity_agent_core"}), sorted(loaded)
