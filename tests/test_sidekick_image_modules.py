"""The sidekick-devops image contains every local module its server imports.

The Dockerfile copies files one by one, and `federation.py` was not among them: the image built, and the server crashed at start
(`ModuleNotFoundError: federation`) whenever SIDEKICK_MODULES was set, which is how an external demo federates its operators.
"""
import ast
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "sidekick-devops"


def _local_imports(path: pathlib.Path, seen=None) -> set:
    """Names of sibling modules imported anywhere in `path` (also inside functions), followed transitively."""
    seen = set() if seen is None else seen
    for node in ast.walk(ast.parse(path.read_text())):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module and node.level == 0 else []
        for name in names:
            top = name.split(".")[0]
            sibling = HERE / f"{top}.py"
            if sibling.exists() and top not in seen:
                seen.add(top)
                _local_imports(sibling, seen)
    return seen


def test_the_dockerfile_copies_every_sibling_module_the_server_imports():
    dockerfile = (HERE / "Dockerfile").read_text().replace("\\\n", " ")
    copied = set(re.findall(r"deploy/sidekick-devops/([A-Za-z_0-9]+)\.py", dockerfile))
    needed = _local_imports(HERE / "sidekick_server.py") | {"sidekick_server"}
    assert "federation" in needed, "the server no longer imports federation: update this test's premise"
    assert needed <= copied, f"imported by the server but not copied into the image: {sorted(needed - copied)}"
