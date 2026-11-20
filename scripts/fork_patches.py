#!/usr/bin/env python3
"""Report the fork's patch queue and the upstream state of every patch.

Reads these trailers from each non-merge commit in BASE..HEAD (see FORK.md):

  Upstream-PR: <number> | none
  Upstream-PR-Head: <upstream PR head SHA at pick time>
  Upstream-Issue: <number>[, <number>...]
  Fork-Only: <reason>

Upstream state comes from `gh api`; pass --offline to validate trailers only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

UPSTREAM_REPO = "Soju06/codex-lb"
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
TRAILER_KEYS = ("Upstream-PR", "Upstream-PR-Head", "Upstream-Issue", "Fork-Only")


@dataclass
class Patch:
    sha: str
    subject: str
    prs: list[str]
    pr_heads: list[str]
    issues: list[str]
    fork_only: list[str]
    status: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


def _run(*args: str) -> str:
    result = subprocess.run(list(args), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def _is_number(value: str) -> bool:
    return re.fullmatch(r"[0-9]+", value) is not None


def read_patches(base: str, head: str) -> list[Patch]:
    trailers = "".join(f"{FIELD_SEP}%(trailers:key={key},valueonly,separator=%x2C)" for key in TRAILER_KEYS)
    log_format = f"--format=%h{FIELD_SEP}%s{trailers}{RECORD_SEP}"
    output = _run("git", "log", "--reverse", "--no-merges", log_format, f"{base}..{head}")
    patches: list[Patch] = []
    for record in output.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, prs, heads, issues, fork_only = record.split(FIELD_SEP)
        patches.append(Patch(sha, subject, _split(prs), _split(heads), _split(issues), _split(fork_only)))
    return patches


class Upstream:
    def __init__(self, repo: str) -> None:
        self.repo = repo
        self._cache: dict[str, dict[str, Any]] = {}

    def _get(self, path: str) -> dict[str, Any]:
        if path not in self._cache:
            payload = json.loads(_run("gh", "api", f"repos/{self.repo}/{path}"))
            if not isinstance(payload, dict):
                raise SystemExit(f"unexpected payload for {path}")
            self._cache[path] = payload
        return self._cache[path]

    def pull(self, number: str) -> dict[str, Any]:
        return self._get(f"pulls/{number}")

    def issue(self, number: str) -> dict[str, Any]:
        return self._get(f"issues/{number}")


def validate(patch: Patch) -> None:
    if not patch.prs:
        patch.signals.append("missing Upstream-PR trailer")
        return
    for value in patch.prs:
        if value != "none" and not _is_number(value):
            patch.signals.append(f"invalid Upstream-PR value {value!r}")
    if patch.prs == ["none"] and not (patch.issues or patch.fork_only):
        patch.signals.append("Upstream-PR none needs Upstream-Issue or Fork-Only")
    for value in patch.issues:
        if not _is_number(value):
            patch.signals.append(f"invalid Upstream-Issue value {value!r}")


def inspect(patch: Patch, upstream: Upstream) -> None:
    for number in (value for value in patch.prs if _is_number(value)):
        pull = upstream.pull(number)
        head_sha = str(pull["head"]["sha"])
        if pull.get("merged_at"):
            patch.status.append(f"PR {number} merged")
            patch.signals.append(f"PR {number} merged upstream: drop on rebuild")
        elif pull["state"] == "closed":
            patch.status.append(f"PR {number} closed")
            patch.signals.append(f"PR {number} closed without merge: review")
        else:
            patch.status.append(f"PR {number} open")
            if not patch.pr_heads:
                patch.signals.append(f"PR {number}: Upstream-PR-Head missing")
            elif not any(head_sha.startswith(recorded) for recorded in patch.pr_heads):
                patch.signals.append(f"PR {number} head moved to {head_sha[:10]}: review")
    for number in (value for value in patch.issues if _is_number(value)):
        issue = upstream.issue(number)
        if issue["state"] == "closed":
            reason = issue.get("state_reason") or "closed"
            patch.status.append(f"issue {number} {reason}")
            patch.signals.append(f"issue {number} closed upstream ({reason}): review")
        else:
            patch.status.append(f"issue {number} open")
    if patch.fork_only:
        patch.status.append(f"fork-only: {', '.join(patch.fork_only)}")


def render(patches: list[Patch], base: str, head: str) -> str:
    lines = [
        f"Fork patches {base}..{head}: {len(patches)} commit(s)",
        "",
        "| Patch | Subject | Upstream PR | Upstream issue | Upstream state | Signals |",
        "|---|---|---|---|---|---|",
    ]
    for patch in patches:
        cells = [
            f"`{patch.sha}`",
            patch.subject.replace("|", "\\|"),
            ", ".join(patch.prs) or "-",
            ", ".join(patch.issues) or "-",
            "; ".join(patch.status) or "-",
            "; ".join(patch.signals) or "ok",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="upstream/main", help="upstream ref the queue sits on")
    parser.add_argument("--head", default="HEAD", help="patched ref to report")
    parser.add_argument("--repo", default=UPSTREAM_REPO, help="upstream repository, owner/name")
    parser.add_argument("--offline", action="store_true", help="validate trailers without GitHub lookups")
    parser.add_argument("--fail-on-signal", action="store_true", help="exit 1 when any patch needs attention")
    args = parser.parse_args()

    patches = read_patches(args.base, args.head)
    upstream = Upstream(args.repo)
    for patch in patches:
        validate(patch)
        if not args.offline:
            inspect(patch, upstream)
    print(render(patches, args.base, args.head))
    if args.fail_on_signal and any(patch.signals for patch in patches):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
