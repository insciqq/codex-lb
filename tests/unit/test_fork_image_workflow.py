from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.modules.runtime.release_version import parse_release_tag

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fork-image.yml"
CI_STEP = "Require a successful CI run for the commit"
TRUST_STEPS = [
    "Require a release tag ref",
    "Checkout tag",
    "Require the run commit to be the tag commit",
    "Require the commit on the fork's main and not upstream's",
    CI_STEP,
]


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _verify_steps() -> list[dict[str, Any]]:
    return _workflow()["jobs"]["verify"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _verify_steps() if step.get("name") == name)


def test_verify_runs_nothing_from_the_checkout_before_the_trust_checks() -> None:
    steps = _verify_steps()
    names = [step.get("name") for step in steps]
    ci_index = names.index(CI_STEP)
    assert names[: ci_index + 1] == TRUST_STEPS
    for step in steps[: ci_index + 1]:
        run = step.get("run", "")
        assert "python" not in run, step["name"]
        assert ".github/scripts" not in run, step["name"]
        assert "app." not in run and "app/" not in run, step["name"]
        assert step.get("uses", "actions/checkout@").startswith("actions/checkout@"), step["name"]


def test_fork_image_workflow_never_writes_latest() -> None:
    workflow = _workflow()
    assert set(workflow["jobs"]) == {"verify", "build", "attest", "tag"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert ":latest" not in text
    assert "ghcr_point_latest" not in text


def _run_step(name: str, cwd: Path, env: dict[str, str], tmp_path: Path) -> tuple[int, dict[str, str]]:
    output = tmp_path / f"output-{len(list(tmp_path.iterdir()))}"
    output.touch()
    result = subprocess.run(
        ["bash", "-c", _step(name)["run"]],
        cwd=cwd,
        env={**os.environ, **env, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
    return result.returncode, values


@pytest.mark.parametrize(
    "tag",
    [
        "v1.25.0-fork.2",
        "v1.26.0",
        "v0.0.0",
        "v1.25.0-fork.0",
        "v1.26",
        "v01.26.0",
        "1.26.0",
        "v1.2５.0",
        "v1.26.0-beta.1",
        "v1.26.0\nv1.25.0",
    ],
)
def test_tag_step_accepts_exactly_the_module_release_tags(tag: str, tmp_path: Path) -> None:
    returncode, values = _run_step(
        "Require a release tag ref", tmp_path, {"RELEASE_TAG": tag, "REF_TYPE": "tag", "LC_ALL": "C"}, tmp_path
    )
    release = parse_release_tag(tag)
    if release is None:
        assert returncode != 0
        assert values == {}
    else:
        assert returncode == 0
        assert values == {"version": release.semver, "pep440": release.pep440}


def test_tag_step_requires_a_tag_ref(tmp_path: Path) -> None:
    returncode, _ = _run_step(
        "Require a release tag ref", tmp_path, {"RELEASE_TAG": "main", "REF_TYPE": "branch"}, tmp_path
    )
    assert returncode != 0


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(cwd: Path, message: str) -> str:
    _git(cwd, "commit", "--allow-empty", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("target", "accepted"),
    [("fork", True), ("upstream", False), ("off-main", False)],
)
def test_ancestry_steps_accept_only_fork_main_commits(target: str, accepted: bool, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    upstream = _commit(repo, "upstream")
    fork = _commit(repo, "fork")
    _git(repo, "update-ref", "refs/remotes/origin/upstream-main", upstream)
    _git(repo, "update-ref", "refs/remotes/origin/main", fork)
    _git(repo, "switch", "-q", "--detach", upstream)
    off_main = _commit(repo, "off main")
    sha = {"fork": fork, "upstream": upstream, "off-main": off_main}[target]
    _git(repo, "tag", "v1.26.0", sha)

    env = {"RELEASE_TAG": "v1.26.0", "GITHUB_SHA": sha}
    returncode, values = _run_step("Require the run commit to be the tag commit", repo, env, tmp_path)
    assert (returncode, values) == (0, {"sha": sha})
    returncode, _ = _run_step("Require the commit on the fork's main and not upstream's", repo, {"SHA": sha}, tmp_path)
    assert (returncode == 0) is accepted

    returncode, _ = _run_step(
        "Require the run commit to be the tag commit", repo, {**env, "GITHUB_SHA": "0" * 40}, tmp_path
    )
    assert returncode != 0
