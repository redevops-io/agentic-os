"""The release script stops at the first failed check, and publishes nothing whose downloaded assets did not pass."""
import importlib.util
import pathlib
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cut_release", ROOT / "scripts" / "cut_release.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def a_run(name, conclusion="success", status="completed", at="2026-01-01T00:00:00Z", number=1, path=".github/workflows/x.yml"):
    return {"name": name, "status": status, "conclusion": conclusion, "created_at": at, "id": number, "path": path}


def test_only_a_commit_whose_every_run_passed_can_be_released():
    good = [a_run("tests"), a_run("integration-checks"), a_run("docs-check", "skipped")]
    assert R.run_problems(good) == []
    assert R.run_problems([a_run("tests")]) == ["integration-checks: has not run on this commit"]
    assert R.run_problems(good + [a_run("secret-scan", "failure")]) == ["secret-scan: failure"]
    assert R.run_problems(good + [a_run("native-core", None, "in_progress")]) == ["native-core: still in_progress"]
    assert R.run_problems([a_run("tests", "skipped"), a_run("integration-checks")]) == ["tests: skipped, and it is required"]


def test_the_newest_run_of_a_workflow_counts_and_the_image_build_is_not_a_precondition():
    runs = [a_run("tests", "failure", at="2026-01-01T00:00:00Z"), a_run("tests", at="2026-01-01T01:00:00Z", number=2),
            a_run("integration-checks"), a_run("oci-release", "failure", path=".github/workflows/oci-release.yml")]
    assert R.run_problems(runs) == []
    runs.append(a_run("tests", "failure", at="2026-01-01T02:00:00Z", number=3))
    assert R.run_problems(runs) == ["tests: failure"]


def test_a_failing_command_ends_the_release():
    with pytest.raises(R.ReleaseError, match="exit status 3"):
        R.run([sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(R.ReleaseError, match="did not run"):
        R.run(["no-such-command-here"])


def test_a_probe_that_exits_cleanly_without_its_verdict_fails(tmp_path):
    (tmp_path / "node_modules" / "pyodide").mkdir(parents=True)
    with pytest.raises(R.ReleaseError, match="without saying"):
        R.probe(tmp_path / "a.zip", ROOT, tmp_path, runner=lambda cmd, **kw: "loading...\n")


def an_archive(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)


def assets(tmp_path, files, listed=None):
    folder = tmp_path / "assets"
    folder.mkdir()
    an_archive(folder / R.ASSET, files)
    (folder / R.CHECKSUM).write_text(f"{listed or R.sha256(folder / R.ASSET)}  {R.ASSET}\n")
    return folder


def test_downloaded_assets_must_match_their_checksum_and_the_tagged_source_and_run(tmp_path, monkeypatch):
    probed = []
    monkeypatch.setattr(R, "build", lambda source, out, runner: an_archive(out, {"agentic_os/a.py": "1"}))
    monkeypatch.setattr(R, "probe", lambda archive, source, work, runner: probed.append(archive))
    folder = assets(tmp_path, {"agentic_os/a.py": "1"})
    assert R.check_assets(folder, ROOT, tmp_path / "work") == R.sha256(folder / R.ASSET) and probed == [folder / R.ASSET]
    (folder / R.CHECKSUM).write_text("0" * 64)
    with pytest.raises(R.ReleaseError, match="does not match"):
        R.check_assets(folder, ROOT, tmp_path / "work")
    an_archive(folder / R.ASSET, {"agentic_os/a.py": "something else"})
    (folder / R.CHECKSUM).write_text(R.sha256(folder / R.ASSET))
    with pytest.raises(R.ReleaseError, match="tagged source"):
        R.check_assets(folder, ROOT, tmp_path / "work")
    (folder / R.CHECKSUM).unlink()
    with pytest.raises(R.ReleaseError, match="does not carry both"):
        R.check_assets(folder, ROOT, tmp_path / "work")
    assert len(probed) == 1


@pytest.fixture
def publishing(monkeypatch, tmp_path):
    """publish() with every external step recorded; `fails` names the step that raises."""
    commands, steps, fails = [], [], set()

    def step(name, result=None):
        def call(*args, **kwargs):
            steps.append(name)
            if name in fails:
                raise R.ReleaseError(name + " failed")
            return result(*args) if callable(result) else result
        monkeypatch.setattr(R, name, call)

    step("check", lambda tag, runner, commit="": commit or "c" * 40)
    step("export", lambda commit, into, *a: into)
    step("build", lambda source, out, *a: an_archive(out, {"a": "1"}))
    step("probe")
    step("download", lambda tag, into, *a: into)
    step("check_assets", lambda *a: "d" * 64)
    step("verify")
    step("images")
    monkeypatch.setattr(R, "sha256", lambda p: "d" * 64)

    def runner(cmd, **kwargs):
        commands.append(" ".join(cmd))
        return ""
    notes = tmp_path / "notes.md"
    notes.write_text("what changed")
    return {"go": lambda: R.publish("v9.9.9", notes, "", runner), "commands": commands, "steps": steps, "fails": fails}


def test_publishing_goes_draft_then_downloaded_check_then_public_then_images(publishing):
    publishing["go"]()
    said = publishing["commands"]
    create = next(i for i, c in enumerate(said) if c.startswith("gh release create"))
    public = next(i for i, c in enumerate(said) if "--draft=false" in c)
    assert "--draft" in said[create].split() and create < public
    assert publishing["steps"] == ["check", "export", "build", "probe", "download", "check_assets", "verify", "images"]


@pytest.mark.parametrize("broken,never", [
    ("check", ("git tag", "git push", "gh release")),
    ("probe", ("git tag", "git push", "gh release")),
    ("check_assets", ("--draft=false",)),
    ("verify", ()),
])
def test_a_failed_step_stops_everything_after_it(publishing, broken, never):
    publishing["fails"].add(broken)
    with pytest.raises(R.ReleaseError):
        publishing["go"]()
    for words in never:
        assert not [c for c in publishing["commands"] if words in c], (broken, words)
    assert publishing["steps"][-1] == broken and "images" not in publishing["steps"]


def a_git(contained=True, head="h" * 40):
    said = []

    def runner(cmd, **kwargs):
        said.append(" ".join(cmd))
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"] and not contained:
            raise R.ReleaseError("not an ancestor")
        if cmd[:2] == ["git", "rev-parse"]:
            return (head if cmd[-1] == "origin/main^{commit}" else cmd[-1].split("^")[0]) + "\n"
        return ""
    return runner, said


def test_a_release_is_of_one_commit_contained_in_main_wherever_main_has_got_to():
    runner, _ = a_git()
    assert R.release_commit("a" * 40, runner) == "a" * 40 and R.release_commit("", runner) == "h" * 40
    runner, _ = a_git(contained=False)
    with pytest.raises(R.ReleaseError):
        R.release_commit("a" * 40, runner)


def test_the_candidate_runs_the_dispatched_checks_on_a_branch_held_at_that_commit():
    runner, said = a_git()
    R.candidate("v9.9.9", "a" * 40, runner)
    assert f"git push --quiet origin {'a' * 40}:refs/heads/rc/v9.9.9" in said
    assert any(c.startswith("gh workflow run integration-checks.yml") and c.endswith("--ref rc/v9.9.9") for c in said)


def test_images_are_built_on_the_tag_and_never_depend_on_where_main_is():
    said, runs = [], [[], [{"databaseId": 7}]]

    def runner(cmd, **kwargs):
        said.append(" ".join(cmd))
        if cmd[:3] == ["gh", "run", "list"]:
            return __import__("json").dumps(runs.pop(0) if runs else [{"databaseId": 7}])
        if cmd[:3] == ["gh", "run", "view"]:
            return '{"status": "completed", "conclusion": "success"}'
        return ""
    R.images("v9.9.9", "a" * 40, runner, pause=lambda s: None)
    dispatch = next(c for c in said if c.startswith("gh workflow run oci-release.yml"))
    assert "--ref v9.9.9 " in dispatch and not any("origin/main" in c for c in said)


def test_the_images_workflow_builds_only_a_tag_contained_in_main():
    text = (ROOT / ".github" / "workflows" / "oci-release.yml").read_text()
    assert '[ "$GITHUB_REF" = "refs/tags/$REF" ]' in text and 'git merge-base --is-ancestor "$COMMIT" origin/main' in text


def test_a_later_merge_never_cancels_the_checks_of_a_pushed_commit():
    import yaml
    checked = []
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        group = (doc.get("concurrency") or {})
        on = doc.get(True) or doc.get("on") or {}
        if isinstance(group, dict) and group.get("cancel-in-progress") and ("push" in on or "workflow_dispatch" in on):
            assert "github.sha" in group["group"], path.name
            checked.append(path.name)
    assert {"tests.yml", "native-core.yml", "integration-checks.yml"} <= set(checked)
