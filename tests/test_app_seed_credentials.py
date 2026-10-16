"""App seed scripts must take credentials from the environment, never from a literal default."""
from __future__ import annotations

import re
from pathlib import Path

APPS = Path(__file__).resolve().parent.parent / "apps"
SEEDS = sorted(list(APPS.glob("*/seed.py")) + list(APPS.glob("*/seed.rb")))

# A capitalised word, then a short suffix followed by "!" and digits: the shape of a shared demo password.
_SHAPED = re.compile(r"[A-Z][a-z]+[A-Za-z]*![0-9]{4}")
# NAME_PASSWORD = "literal"  /  environ.get("..._PASSWORD", "literal")
_ASSIGN = re.compile(r"""(?i)password\w*\s*=\s*(['"])[^'"\s]+\1""")
_ENV_DEFAULT = re.compile(r"""(?i)(environ\.get|getenv|ENV\.fetch)\(\s*['"][A-Z_]*(PASSWORD|SECRET|API_KEY)[A-Z_]*['"]\s*,\s*['"][^'"]+['"]""")


def test_seed_scripts_found():
    assert SEEDS


def test_no_seed_contains_a_shared_password_literal():
    for p in SEEDS:
        assert not _SHAPED.search(p.read_text()), p


def test_no_seed_assigns_or_defaults_a_password_literal():
    for p in SEEDS:
        text = p.read_text()
        assert not _ASSIGN.search(text), p
        assert not _ENV_DEFAULT.search(text), p


def test_no_app_source_embeds_a_shared_password_literal():
    for p in APPS.rglob("*"):
        if p.suffix in (".py", ".rb", ".md", ".sh", ".yml", ".yaml") and p.is_file():
            assert not _SHAPED.search(p.read_text(errors="ignore")), p
