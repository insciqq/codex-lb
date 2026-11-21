from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.runtime.release_version import (
    ReleaseVersion,
    highest_release,
    is_newer_release,
    parse_release_tag,
    parse_running_version,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("tag", "semver", "pep440"),
    [
        ("v1.25.0-fork.2", "1.25.0-fork.2", "1.25.0+fork.2"),
        ("v1.25.0-fork.10", "1.25.0-fork.10", "1.25.0+fork.10"),
        ("v1.26.0", "1.26.0", "1.26.0"),
        ("v0.0.0", "0.0.0", "0.0.0"),
    ],
)
def test_release_tag_spellings(tag: str, semver: str, pep440: str) -> None:
    release = parse_release_tag(tag)
    assert release is not None
    assert release.semver == semver
    assert release.pep440 == pep440
    assert release.tag == tag


@pytest.mark.parametrize(
    "tag",
    [
        "1.26.0",
        "1.25.0-fork.2",
        "v1.25.0-fork.0",
        "v1.25.0-fork.02",
        "v1.25.0+fork.1",
        "v1.25.0-beta.1",
        "v1.26",
        "v01.26.0",
        "v1.26.0 ",
        "rebuild/2026-11-21-abc1234",
        "latest",
    ],
)
def test_parse_release_tag_rejects_other_shapes(tag: str) -> None:
    assert parse_release_tag(tag) is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.25.0+fork.2", ReleaseVersion(1, 25, 0, 2)),
        ("1.26.0", ReleaseVersion(1, 26, 0, 0)),
        ("1.25.0-fork.2", None),
        ("1.25.0b2", None),
    ],
)
def test_parse_running_version(version: str, expected: ReleaseVersion | None) -> None:
    assert parse_running_version(version) == expected


def test_releases_order_by_base_then_fork_number() -> None:
    ordered = ["v1.24.9-fork.9", "v1.25.0", "v1.25.0-fork.1", "v1.25.0-fork.2", "v1.25.0-fork.10", "v1.26.0"]
    releases = [release for tag in ordered if (release := parse_release_tag(tag)) is not None]
    assert len(releases) == len(ordered)
    assert releases == sorted(releases)
    assert len(set(releases)) == len(releases)


@pytest.mark.parametrize(
    ("tag", "running", "newer"),
    [
        ("v1.26.0", "1.25.0+fork.9", True),
        ("v1.25.0-fork.2", "1.25.0", True),
        ("v1.25.0", "1.25.0+fork.2", False),
        ("v1.26.0", "1.26.0", False),
        ("v1.26.0", "not-a-version", False),
        ("latest", "1.25.0", False),
    ],
)
def test_is_newer_release(tag: str, running: str, newer: bool) -> None:
    assert is_newer_release(tag, running) is newer


def test_highest_release_ignores_other_tags() -> None:
    tags = ["v1.25.0-fork.1", "v1.25.0-fork.2", "rebuild/2026-11-21-abc1234", "v9.9.9-beta.1", "latest"]
    assert highest_release(tags) == ReleaseVersion(1, 25, 0, 2)
    assert highest_release([*tags, "v1.26.0"]) == ReleaseVersion(1, 26, 0, 0)
    assert highest_release(["latest"]) is None


def _cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.modules.runtime.release_version", *args],
        cwd=REPO_ROOT,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_prints_forms() -> None:
    fork = _cli("forms", "v1.25.0-fork.2")
    plain = _cli("forms", "v1.26.0")
    assert (fork.returncode, fork.stdout) == (0, "semver=1.25.0-fork.2\npep440=1.25.0+fork.2\n")
    assert (plain.returncode, plain.stdout) == (0, "semver=1.26.0\npep440=1.26.0\n")


@pytest.mark.parametrize("args", [("forms", "v1.25.0-fork.0"), ("is-highest", "1.26.0"), ("forms",), ("x", "v1.0.0")])
def test_cli_rejects_bad_input(args: tuple[str, ...]) -> None:
    result = _cli(*args)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("tag", "existing", "highest", "is_highest"),
    [
        # Re-running an older release never makes it the highest.
        ("v1.25.0-fork.1", "v1.25.0-fork.1\nv1.25.0-fork.2\n", "v1.25.0-fork.2", "false"),
        ("v1.25.0-fork.2", "v1.25.0-fork.1\nv1.25.0-fork.2\n", "v1.25.0-fork.2", "true"),
        ("v1.26.0", "v1.25.0-fork.9\n", "v1.26.0", "true"),
        ("v1.25.0-fork.9", "v1.26.0\nv1.25.0-fork.9\n", "v1.26.0", "false"),
        # The tag counts even when the listing does not include it yet.
        ("v1.25.0-fork.3", "v1.25.0-fork.2\nrebuild/2026-11-21-abc1234\n", "v1.25.0-fork.3", "true"),
    ],
)
def test_cli_is_highest(tag: str, existing: str, highest: str, is_highest: str) -> None:
    result = _cli("is-highest", tag, stdin=existing)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"highest={highest}\nis_highest={is_highest}\n"
