"""No installation starts on a password anyone can read in this repository, and none has its own replaced."""
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "deploy" / "apps"
BUNDLE = ("POSTIZ_JWT_SECRET", "TWENTY_DB_PASSWORD", "METABASE_DB_PASSWORD", "POSTIZ_DB_PASSWORD")
SECRET_NAME = r"[A-Z0-9_]*(?:PASSWORD|_SECRET|SECRET_KEY|_PASS)[A-Z0-9_]*"


def deploy_files():
    for pattern in ("deploy/**/*.yml", "deploy/**/*.yaml", "deploy/**/.env*.example", "deploy/**/*.env.example"):
        yield from ROOT.glob(pattern)


def test_no_deploy_file_falls_back_to_a_literal_password():
    found = [f"{p.relative_to(ROOT)}: {m.group(1)}" for p in set(deploy_files())
             for m in re.finditer(r"\$\{(" + SECRET_NAME + r"):?-[^}$]+\}", p.read_text())]
    assert not found, found


def test_no_env_example_ships_a_password():
    found = [f"{p.relative_to(ROOT)}: {m.group(1)}" for p in set(deploy_files()) if p.name.endswith("example")
             for m in re.finditer(r"(?m)^(" + SECRET_NAME + r")=[ \t]*[^\s#<]", p.read_text())]
    assert not found, found


@pytest.mark.parametrize("compose", ["deploy/apps/compose.yml", "deploy/sidekick-devops/docker-compose.yml"])
def test_a_missing_password_stops_compose_instead_of_being_filled_in(compose):
    text = (ROOT / compose).read_text()
    names = BUNDLE[1:] if "apps" in compose else ("GRAFANA_ADMIN_PASSWORD",)
    for name in names:
        uses = re.findall(r"\$\{" + name + r"([^}]*)\}", text)
        assert uses and all(u.startswith(":?") for u in uses), (name, uses)


posix = pytest.mark.skipif(sys.platform.startswith("win") or not shutil.which("sh"), reason="needs a posix shell")


def run(tmp_path, volumes=(), docker_fails=False, env=None, bundle_text=None):
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text("#!/bin/sh\nexit 1\n" if docker_fails else "#!/bin/sh\n" + "".join(f"echo {v}\n" for v in volumes) + "exit 0\n")
    fake.chmod(0o755)
    bundle = tmp_path / "bundle.env"
    if bundle_text is not None:
        bundle.write_text(bundle_text)
    full = {"HOME": str(tmp_path), "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "REDEVOPS_BUNDLE_ENV": str(bundle), **(env or {})}
    done = subprocess.run(["sh", str(APPS / "bundle-passwords.sh")], env=full, capture_output=True, text=True)
    values = dict(line.split("=", 1) for line in bundle.read_text().splitlines() if "=" in line and not line.startswith("#"))
    return done, values, bundle


@posix
def test_a_new_install_gets_its_own_passwords_once_and_they_are_never_printed(tmp_path):
    first, values, bundle = run(tmp_path)
    assert first.returncode == 0, first.stderr
    made = [values[n] for n in BUNDLE]
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in made) and len(set(made)) == 4
    assert not any(v in first.stdout + first.stderr for v in made)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert "TENANT=" in bundle.read_text()                                    # started from .env.example
    again, after, _ = run(tmp_path)
    assert [after[n] for n in BUNDLE] == made and again.stdout.count("already-set") == 4
    other = tmp_path / "other"
    other.mkdir()
    assert run(other)[1]["TWENTY_DB_PASSWORD"] != values["TWENTY_DB_PASSWORD"]


@posix
def test_a_database_that_already_exists_gets_no_new_password(tmp_path):
    done, values, _ = run(tmp_path, volumes=["apps_twenty_db", "apps_postiz_db", "elsewhere_metabase_db"])
    assert done.returncode == 1
    assert not values.get("TWENTY_DB_PASSWORD") and not values.get("POSTIZ_DB_PASSWORD")
    assert values["METABASE_DB_PASSWORD"] and values["POSTIZ_JWT_SECRET"]      # another project's volume says nothing about ours
    assert "apps_twenty_db exists" in done.stderr


@posix
def test_the_project_name_chosen_in_env_is_the_one_checked(tmp_path):
    done, values, _ = run(tmp_path, volumes=["shop_twenty_db"], bundle_text="COMPOSE_PROJECT_NAME=shop\nTWENTY_DB_PASSWORD=\n")
    assert done.returncode == 1 and not values["TWENTY_DB_PASSWORD"]


@posix
def test_when_docker_cannot_be_asked_no_database_password_is_made_up(tmp_path):
    done, values, _ = run(tmp_path, docker_fails=True)
    assert done.returncode == 1 and not any(values.get(n) for n in BUNDLE[1:]) and values["POSTIZ_JWT_SECRET"]


@posix
def test_values_already_chosen_are_kept_wherever_compose_would_read_them(tmp_path):
    secrets = tmp_path / ".config" / "redevops" / "secrets.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("METABASE_DB_PASSWORD=from-the-secrets-file\n")
    text = "KEEP=this line\nTWENTY_DB_PASSWORD=chosen-by-hand with $dollar and \\slash\nPOSTIZ_DB_PASSWORD=\n"
    done, values, bundle = run(tmp_path, volumes=["apps_twenty_db", "apps_metabase_db"], env={"POSTIZ_JWT_SECRET": "from-the-environment"}, bundle_text=text)
    assert done.returncode == 0, done.stderr
    assert values["TWENTY_DB_PASSWORD"] == "chosen-by-hand with $dollar and \\slash" and values["KEEP"] == "this line"
    assert "METABASE_DB_PASSWORD" not in values and "POSTIZ_JWT_SECRET" not in values
    assert len(values["POSTIZ_DB_PASSWORD"]) == 64 and bundle.read_text().count("POSTIZ_DB_PASSWORD=") == 1
