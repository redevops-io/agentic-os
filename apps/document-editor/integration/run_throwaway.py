#!/usr/bin/env python3
"""Run the integration suite against a THROWAWAY stock ONLYOFFICE Document Server container.

* image is pinned by DIGEST (``PINNED_IMAGE`` next to this file); JWT enabled with a random throwaway secret;
* published on 127.0.0.1 on a random port only; ``--memory 6g``;
* the container AND its anonymous volumes are removed on exit, success or failure;
* never touches any production host. Pure Python: works wherever Docker does.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def sh(*a, **k):
    return subprocess.run(a, check=k.pop("check", True), capture_output=True, text=True, **k)


def main() -> int:
    image = (HERE / "PINNED_IMAGE").read_text().split()[0]
    name = "docedit-it-" + secrets.token_hex(4)
    secret = secrets.token_hex(24)
    host_addr = os.environ.get("DOCEDITOR_IT_HOST_ADDR", "172.17.0.1")
    sh("docker", "run", "-d", "--name", name, "--memory", "6g", "-p", "127.0.0.1::80",
       "-e", "JWT_ENABLED=true", "-e", f"JWT_SECRET={secret}",
       "-e", "ALLOW_PRIVATE_IP_ADDRESS=true", "-e", "ALLOW_META_IP_ADDRESS=true", image)
    try:
        port = sh("docker", "port", name, "80/tcp").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if urllib.request.urlopen(url + "/healthcheck", timeout=3).read().strip() == b"true":
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        else:
            print("document server did not become healthy", file=sys.stderr)
            return 2
        time.sleep(5)
        env = dict(os.environ, DOCEDITOR_IT_ONLYOFFICE_URL=url, DOCEDITOR_IT_ONLYOFFICE_JWT_SECRET=secret,
                   DOCEDITOR_IT_HOST_ADDR=host_addr,
                   PYTHONPATH=os.pathsep.join([str(REPO), str(REPO / "apps")]))
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "integration",
               str(HERE), *sys.argv[1:]]
        return subprocess.call(cmd, env=env, cwd=str(REPO))
    finally:
        sh("docker", "rm", "-f", "-v", name, check=False)


if __name__ == "__main__":
    sys.exit(main())
