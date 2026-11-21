"""Fork release versions: parsing, spellings and ordering.

A fork release tag is either ``v<major>.<minor>.<patch>-fork.<n>`` (an upstream
base plus the fork's fixes) or a plain ``v<major>.<minor>.<patch>`` (a release
of the fork's own). Python and the dashboard carry the PEP 440 form
(``X.Y.Z+fork.N`` or ``X.Y.Z``); image tags and the Helm chart carry the SemVer
form (``X.Y.Z-fork.N`` or ``X.Y.Z``). Releases compare as the numeric tuple
``(major, minor, patch, fork)`` where a plain version has fork 0, so
``1.26.0 > 1.25.0-fork.9`` and ``1.25.0-fork.2 > 1.25.0``.

The module is stdlib-only: the Fork image workflow runs it from a checkout with
``python3 -m app.modules.runtime.release_version`` and the app uses it for the
update notice, so both order releases the same way.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass

_CORE = r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
_RELEASE_TAG_RE = re.compile(rf"^v{_CORE}(?:-fork\.(?P<fork>[1-9]\d*))?$")
_RUNNING_VERSION_RE = re.compile(rf"^v?{_CORE}(?:\+fork\.(?P<fork>[1-9]\d*))?$")


@dataclass(frozen=True, slots=True, order=True)
class ReleaseVersion:
    major: int
    minor: int
    patch: int
    fork: int = 0

    @property
    def semver(self) -> str:
        """Image tag and Helm chart spelling."""
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-fork.{self.fork}" if self.fork else core

    @property
    def pep440(self) -> str:
        """``pyproject.toml``, ``app.__version__`` and dashboard spelling."""
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}+fork.{self.fork}" if self.fork else core

    @property
    def tag(self) -> str:
        return f"v{self.semver}"


def _from_match(match: re.Match[str] | None) -> ReleaseVersion | None:
    if match is None:
        return None
    fork = match.group("fork")
    return ReleaseVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        int(fork) if fork else 0,
    )


def parse_release_tag(tag: str) -> ReleaseVersion | None:
    """Parse ``vX.Y.Z`` or ``vX.Y.Z-fork.N``; anything else is not a release tag."""
    return _from_match(_RELEASE_TAG_RE.match(tag))


def parse_running_version(version: str) -> ReleaseVersion | None:
    """Parse the running ``X.Y.Z`` or ``X.Y.Z+fork.N``; anything else is unknown."""
    return _from_match(_RUNNING_VERSION_RE.match(version.strip()))


def is_newer_release(tag: str, running_version: str) -> bool:
    candidate = parse_release_tag(tag)
    current = parse_running_version(running_version)
    if candidate is None or current is None:
        return False
    return candidate > current


def highest_release(tags: Iterable[str]) -> ReleaseVersion | None:
    """The highest release among ``tags``; tags of any other shape are ignored."""
    releases = [release for tag in tags if (release := parse_release_tag(tag.strip())) is not None]
    return max(releases, default=None)


def _main(argv: list[str]) -> int:
    usage = (
        "usage: python3 -m app.modules.runtime.release_version forms <tag>\n"
        "       python3 -m app.modules.runtime.release_version is-highest <tag> < tags"
    )
    if len(argv) != 2 or argv[0] not in {"forms", "is-highest"}:
        print(usage, file=sys.stderr)
        return 2
    command, tag = argv
    release = parse_release_tag(tag)
    if release is None:
        print(f"{tag!r} is not a release tag: expected vX.Y.Z or vX.Y.Z-fork.N", file=sys.stderr)
        return 1
    if command == "forms":
        print(f"semver={release.semver}")
        print(f"pep440={release.pep440}")
        return 0
    highest = highest_release([*sys.stdin.read().split(), tag])
    assert highest is not None
    print(f"highest={highest.tag}")
    print(f"is_highest={'true' if highest == release else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
