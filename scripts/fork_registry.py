#!/usr/bin/env python3
"""Public registry of what the fork did with each upstream pull request and issue.

Two sources, nothing else:

* Commit trailers of every non-merge commit in BASE..HEAD (see FORK.md, read with
  scripts/fork_patches.py). A PR named in `Upstream-PR` is taken; an issue named in
  `Upstream-Issue` is fixed. Trailers are the only source of these two statuses.
* fork/decisions.toml for every other decision. fork/REGISTRY.md is generated
  from both and committed.

Commands:

  render          validate, rewrite fork/decisions.toml canonically, write fork/REGISTRY.md
  check           offline validation for CI. Exit 1 when the data does not parse, is not
                  canonical, has an invalid entry, contradicts the trailers or cannot be
                  rendered, or when fork/REGISTRY.md is missing or not tracked by git.
                  A stale fork/REGISTRY.md and trailer problems are only warnings (new
                  trailer commits land all the time); run `render` to refresh it.
  check --strict  before a release: also fail on a stale document, on any `pending` entry
                  and on commits with missing or invalid trailers.
  sync            online (gh api, read-only): add every open upstream PR and issue, PRs
                  closed without merge since `closed_since` and everything the trailers
                  reference as `pending` entries; refresh title and state of entries that
                  changed upstream since the previous snapshot (`--full`: since
                  `closed_since`); reconcile entries the trailers no longer back (after a
                  rebuild); report state changes; rewrite the data file canonically.

Data schema (TOML, no comments; `render` and `sync` write the canonical form: tables
sorted by number, keys in the order below, empty lists omitted, lists sorted):

  [registry]
  upstream = "Soju06/codex-lb"
  base = "<40-hex sha>"       upstream main commit the patch queue sits on (BASE)
  snapshot = YYYY-MM-DD       date of the upstream state recorded in the entries
  closed_since = YYYY-MM-DD   PRs closed without merge on or after this date are covered

  [pr.<number>]
  title = "..."               upstream title (sync)
  state = "open"              upstream state at snapshot: open | closed | merged (sync)
  status = "pending"          required; a trailer-taken PR stores pending (merged-upstream
                              once upstream merged it; it still renders as taken)
  by = <number>               superseded only: the replacing PR (an entry or a taken PR)
  issues = [<number>, ...]    linked upstream issues
  reason = "..."              public, single-line, neutral English
  rework = "..."              taken PRs only: public note on the fork's changes

  [issue.<number>]
  title = "..."
  state = "open"              open | closed
  status = "pending"          required; pending or partial when trailers reference it
  prs = [<number>, ...]       linked upstream PRs
  reason = "..."

PR statuses set in data: pending, rejected (reason), out-of-scope, not-applicable
(reason), superseded (by), own-fix (issues or reason), merged-upstream (state merged).
Derived: taken, or taken with changes when the entry has `rework`.

Issue statuses set in data: pending, fixed-upstream, not-applicable (reason),
out-of-scope, not-fixed (reason), partial (reason; only for an issue the trailers
reference). Derived: fixed.

`sync` reconciles: a pending PR merged upstream becomes merged-upstream (a taken one
renders as taken with a drop-on-rebuild note until the rebuild drops the pick); after a
rebuild an orphaned `rework` is removed and an orphaned `partial` becomes pending. Entries
recorded as open that no listing returns are refetched (at most 50 per run).

Own changes: commits with `Upstream-PR: none` except those whose subject type is
docs, test, ci or style, or whose `Fork-Only` is tooling, policy, ci or spec-fix.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.fork_patches import Patch, read_patches  # noqa: E402
from scripts.fork_patches import validate as validate_trailers  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = Path("fork/decisions.toml")
DOC_PATH = Path("fork/REGISTRY.md")
COMMAND = "python3 scripts/fork_registry.py"

REGISTRY_KEYS = ("upstream", "base", "snapshot", "closed_since")
PR_KEYS = ("title", "state", "status", "by", "issues", "reason", "rework")
ISSUE_KEYS = ("title", "state", "status", "prs", "reason")
KIND_KEYS = {"pr": PR_KEYS, "issue": ISSUE_KEYS}
INT_LIST_KEYS = {"issues", "prs"}
INT_KEYS = {"by"}
STATES = {"pr": ("open", "closed", "merged"), "issue": ("open", "closed")}

PR_LEGEND = {
    "taken": "ported from the upstream PR into the fork",
    "taken with changes": "ported, with the fork's changes noted",
    "own-fix": "not taken; the problem is fixed by the fork's own commits",
    "rejected": "reviewed and not taken, for the reason given",
    "superseded": "replaced by another PR",
    "out-of-scope": "a feature or other change outside the fork's scope",
    "not-applicable": "does not concern the fork (upstream release, CI, docs or translation work)",
    "merged-upstream": "merged upstream; the fork gets it with upstream main",
    "pending": "no decision yet",
}
ISSUE_LEGEND = {
    "fixed": "fixed in the fork by a taken PR or the fork's own commits",
    "partial": "partly addressed in the fork; the reason says what is left",
    "fixed-upstream": "fixed in upstream main",
    "not-fixed": "reviewed and not fixed in the fork, for the reason given",
    "out-of-scope": "a feature request",
    "not-applicable": "does not concern the fork",
    "pending": "no decision yet",
}
PR_DATA_STATUSES = ("pending", "rejected", "out-of-scope", "not-applicable", "superseded", "own-fix", "merged-upstream")
ISSUE_DATA_STATUSES = ("pending", "fixed-upstream", "not-applicable", "out-of-scope", "not-fixed", "partial")
REASON_REQUIRED = {"pr": {"rejected", "not-applicable"}, "issue": {"not-applicable", "not-fixed", "partial"}}

OWN_SKIP_TYPES = {"docs", "test", "ci", "style"}
OWN_SKIP_FORK_ONLY = {"tooling", "policy", "ci", "spec-fix"}
FORK_ONLY_LABELS = {"production-fix": "bug found in production or review", "security-gate": "security gate"}
# Untrusted text (upstream titles, commit subjects) must render literally under GFM.
# Backslash escapes cover the inline syntax (emphasis, strikethrough, links, images,
# HTML, entities, table pipes). Placed on the ":" of "://" and the "." of "www." they
# also stop URL and www autolinks, which are recognised while parsing. Email
# autolinks are matched on the parsed text, where escapes are gone, so a zero-width
# space entity after "@" breaks them instead; it renders as nothing.
MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]!<&|~])")
AUTOLINK_SCHEME = re.compile(r":(?=//)")
AUTOLINK_WWW = re.compile(r"(?i)\b(www)\.")
VANISHED_FETCH_LIMIT = 50

Data = dict[str, Any]
Fetch = Callable[[str], Any]


class RegistryError(Exception):
    pass


class NotFound(RegistryError):
    """gh api answered 404 or 410 for a single PR or issue."""


# --- data file -------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_date(value: object) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _is_number(value: str) -> bool:
    return re.fullmatch(r"[0-9]+", value) is not None


def parse_data(text: str) -> tuple[Data, list[str]]:
    """Parse the data file into {"registry": {...}, "pr": {n: {...}}, "issue": {n: {...}}}."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{DATA_PATH}: {exc}") from exc
    errors: list[str] = []
    for key in raw:
        if key not in ("registry", "pr", "issue"):
            errors.append(f"unknown table [{key}]")
    registry = raw.get("registry")
    if not isinstance(registry, dict):
        raise RegistryError(f"{DATA_PATH}: missing [registry] table")
    for key in registry:
        if key not in REGISTRY_KEYS:
            errors.append(f"[registry]: unknown key {key!r}")
    if not isinstance(registry.get("upstream"), str) or "/" not in registry["upstream"]:
        errors.append("[registry]: upstream must be owner/name")
    if not isinstance(registry.get("base"), str) or not re.fullmatch(r"[0-9a-f]{40}", registry["base"]):
        errors.append("[registry]: base must be a full 40-character commit sha")
    for key in ("snapshot", "closed_since"):
        if not _is_date(registry.get(key)):
            errors.append(f"[registry]: {key} must be a date (YYYY-MM-DD)")
    data: Data = {"registry": dict(registry), "pr": {}, "issue": {}}
    for kind, keys in KIND_KEYS.items():
        table = raw.get(kind, {})
        if not isinstance(table, dict):
            errors.append(f"[{kind}] must hold [{kind}.<number>] tables")
            continue
        for number, entry in table.items():
            label = f"[{kind}.{number}]"
            if not re.fullmatch(r"[1-9][0-9]*", number) or not isinstance(entry, dict):
                errors.append(f"{label}: expected a table named by a positive number")
                continue
            for key, value in entry.items():
                if key not in keys:
                    errors.append(f"{label}: unknown key {key!r}")
                elif key in INT_LIST_KEYS:
                    if not isinstance(value, list) or not all(_is_int(item) and item > 0 for item in value):
                        errors.append(f"{label}: {key} must be a list of numbers")
                elif key in INT_KEYS:
                    if not _is_int(value) or value <= 0:
                        errors.append(f"{label}: {key} must be a number")
                elif not isinstance(value, str):
                    errors.append(f"{label}: {key} must be a string")
                elif "\n" in value or "\r" in value:
                    errors.append(f"{label}: {key} must be a single line")
                elif key != "title" and not value.strip():
                    errors.append(f"{label}: {key} must not be empty")
            data[kind][int(number)] = dict(entry)
    return data, errors


def _toml_string(value: str) -> str:
    escapes = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    out = []
    for char in value:
        if char in escapes:
            out.append(escapes[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if _is_int(value):
        return str(value)
    if _is_date(value):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in sorted(set(value))) + "]"
    raise RegistryError(f"cannot serialize {value!r}")


def serialize_data(data: Data) -> str:
    blocks = [["[registry]"] + [f"{key} = {_toml_value(data['registry'][key])}" for key in REGISTRY_KEYS]]
    for kind, keys in KIND_KEYS.items():
        for number in sorted(data[kind]):
            entry = data[kind][number]
            lines = [f"[{kind}.{number}]"]
            for key in keys:
                value = entry.get(key)
                if value is None or value == []:
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
            blocks.append(lines)
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


# --- trailers ----------------------------------------------------------------------


@dataclass
class Trailers:
    patches: list[Patch]
    taken: dict[int, list[Patch]] = field(default_factory=dict)
    issue_prs: dict[int, set[int]] = field(default_factory=dict)
    issue_own: dict[int, list[Patch]] = field(default_factory=dict)
    issue_commits: dict[int, list[Patch]] = field(default_factory=dict)
    own_changes: list[Patch] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)


def _numbers(values: list[str]) -> list[int]:
    return [int(value) for value in values if _is_number(value)]


def _subject_type(subject: str) -> str:
    match = re.match(r"([A-Za-z]+)[(!:]", subject)
    return match.group(1).lower() if match else ""


def is_own_change(patch: Patch) -> bool:
    if patch.prs != ["none"]:
        return False
    if _subject_type(patch.subject) in OWN_SKIP_TYPES:
        return False
    return not any(value in OWN_SKIP_FORK_ONLY for value in patch.fork_only)


def collect_trailers(base: str, head: str) -> Trailers:
    ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", base, head], capture_output=True, check=False)
    if ancestor.returncode != 0:
        raise RegistryError(f"base {base[:12]} is not an ancestor of {head}; update [registry].base or fetch history")
    trailers = Trailers(read_patches(base, head))
    for patch in trailers.patches:
        validate_trailers(patch)
        prs = _numbers(patch.prs)
        issues = _numbers(patch.issues)
        if prs and not patch.pr_heads:
            patch.signals.append(f"Upstream-PR {', '.join(map(str, prs))} without Upstream-PR-Head")
        trailers.signals.extend(f"{patch.sha} {patch.subject}: {signal}" for signal in patch.signals)
        for pr in prs:
            trailers.taken.setdefault(pr, []).append(patch)
        for issue in issues:
            trailers.issue_prs.setdefault(issue, set()).update(prs)
            trailers.issue_commits.setdefault(issue, []).append(patch)
            if not prs:
                trailers.issue_own.setdefault(issue, []).append(patch)
        if is_own_change(patch):
            trailers.own_changes.append(patch)
    return trailers


# --- validation ----------------------------------------------------------------------


def _supersede_cycle(data: Data, number: int) -> bool:
    seen = {number}
    current = data["pr"][number].get("by")
    while current in data["pr"] and data["pr"][current].get("status") == "superseded":
        if current == number:
            return True
        if current in seen:
            return False
        seen.add(current)
        current = data["pr"][current].get("by")
    return False


def validate_data(data: Data, trailers: Trailers) -> list[str]:
    errors: list[str] = []
    for number, entry in sorted(data["pr"].items()):
        label = f"[pr.{number}]"
        status = entry.get("status")
        if entry.get("state") is not None and entry["state"] not in STATES["pr"]:
            errors.append(f"{label}: state must be one of {', '.join(STATES['pr'])}")
        if status is None:
            errors.append(f'{label}: status missing (use status = "pending")')
            continue
        if status not in PR_DATA_STATUSES:
            errors.append(f"{label}: unknown status {status!r}; use one of {', '.join(PR_DATA_STATUSES)}")
            continue
        if "by" in entry and status != "superseded":
            errors.append(f"{label}: by is only valid for superseded")
        if number in trailers.taken:
            if status not in ("pending", "merged-upstream"):
                subject = trailers.taken[number][0].subject
                errors.append(
                    f"{label}: taken by commit {subject!r}; data may only set pending or merged-upstream,"
                    f" not {status!r}"
                )
            elif status == "merged-upstream" and entry.get("state") != "merged":
                errors.append(f'{label}: status merged-upstream requires state = "merged"')
            continue
        if "rework" in entry:
            errors.append(f"{label}: rework is only valid for a taken PR (was the pick dropped? run sync)")
        if status in REASON_REQUIRED["pr"] and not entry.get("reason"):
            errors.append(f"{label}: status {status} requires reason")
        if status == "superseded":
            by = entry.get("by")
            if by is None:
                errors.append(f"{label}: status superseded requires by = <PR number>")
            elif by == number:
                errors.append(f"{label}: a PR cannot supersede itself")
            elif by not in data["pr"] and by not in trailers.taken:
                errors.append(f"{label}: by = {by} must name another PR entry or a taken PR (run sync to add it)")
            elif _supersede_cycle(data, number):
                errors.append(f"{label}: superseded chain loops back to {number}")
        if status == "own-fix" and not (entry.get("issues") or entry.get("reason")):
            errors.append(f"{label}: status own-fix requires issues or reason")
        if status == "merged-upstream" and entry.get("state") != "merged":
            errors.append(f'{label}: status merged-upstream requires state = "merged"')
    for number, entry in sorted(data["issue"].items()):
        label = f"[issue.{number}]"
        status = entry.get("status")
        if entry.get("state") is not None and entry["state"] not in STATES["issue"]:
            errors.append(f"{label}: state must be one of {', '.join(STATES['issue'])}")
        if status is None:
            errors.append(f'{label}: status missing (use status = "pending")')
            continue
        if status not in ISSUE_DATA_STATUSES:
            errors.append(f"{label}: unknown status {status!r}; use one of {', '.join(ISSUE_DATA_STATUSES)}")
            continue
        if number in trailers.issue_prs:
            if status not in ("pending", "partial"):
                errors.append(f"{label}: fixed by trailers; data may only set pending or partial, not {status!r}")
        elif status == "partial":
            errors.append(f"{label}: partial is only valid for an issue the trailers reference (run sync)")
        if status in REASON_REQUIRED["issue"] and not entry.get("reason"):
            errors.append(f"{label}: status {status} requires reason")
    return errors


# --- statuses ------------------------------------------------------------------------


def pr_status(number: int, entry: dict[str, Any], trailers: Trailers) -> str:
    if number in trailers.taken:
        return "taken with changes" if entry.get("rework") else "taken"
    return entry.get("status") or "pending"


def issue_status(number: int, entry: dict[str, Any], trailers: Trailers) -> str:
    if number in trailers.issue_prs:
        return "partial" if entry.get("status") == "partial" else "fixed"
    return entry.get("status") or "pending"


def pending_entries(data: Data, trailers: Trailers) -> tuple[list[int], list[int]]:
    prs = [n for n, entry in sorted(data["pr"].items()) if pr_status(n, entry, trailers) == "pending"]
    issues = [n for n, entry in sorted(data["issue"].items()) if issue_status(n, entry, trailers) == "pending"]
    return prs, issues


# --- document --------------------------------------------------------------------


def _title(text: str | None) -> str:
    """Upstream titles and commit subjects: shown literally, never as live markdown."""
    text = MARKDOWN_SPECIAL.sub(r"\\\1", " ".join((text or "").split()))
    text = AUTOLINK_SCHEME.sub(r"\\:", text)
    text = AUTOLINK_WWW.sub(r"\1\\.", text)
    return text.replace("@", "@&#8203;") or "-"


def _text(text: str | None) -> str:
    """Authored reasons and notes: markdown as written, kept inside one table cell."""
    return " ".join((text or "").split()).replace("|", "\\|") or "-"


def render_document(data: Data, trailers: Trailers) -> str:
    registry = data["registry"]
    repo = registry["upstream"]
    url = f"https://github.com/{repo}"

    def pr_link(number: int) -> str:
        return f"[{number}]({url}/pull/{number})"

    def issue_link(number: int) -> str:
        return f"[{number}]({url}/issues/{number})"

    prs = {number: dict(entry) for number, entry in data["pr"].items()}
    for number, patches in trailers.taken.items():
        prs.setdefault(number, {"title": patches[0].subject})
    issues = {number: dict(entry) for number, entry in data["issue"].items()}
    for number in trailers.issue_prs:
        issues.setdefault(number, {})

    pr_issues: dict[int, set[int]] = {number: set(entry.get("issues", [])) for number, entry in prs.items()}
    issue_links: dict[int, set[int]] = {number: set(entry.get("prs", [])) for number, entry in issues.items()}
    for number, patches in trailers.taken.items():
        for patch in patches:
            pr_issues[number].update(_numbers(patch.issues))
    for number, linked in pr_issues.items():
        for issue in linked:
            if issue in issue_links:
                issue_links[issue].add(number)
    for number, linked in issue_links.items():
        for pr in linked:
            if pr in pr_issues:
                pr_issues[pr].add(number)

    pr_rows = []
    pr_counts: dict[str, int] = dict.fromkeys(PR_LEGEND, 0)
    for number in sorted(prs):
        entry = prs[number]
        status = pr_status(number, entry, trailers)
        pr_counts[status] += 1
        notes = []
        if number in trailers.taken and entry.get("state") == "merged":
            notes.append("merged upstream; drop on rebuild")
        if entry.get("rework"):
            notes.append(f"changes: {_text(entry['rework'])}")
        if status == "superseded":
            notes.append(f"superseded by {pr_link(entry['by'])}")
        if entry.get("reason"):
            notes.append(_text(entry["reason"]))
        if pr_issues[number]:
            notes.append("issues " + ", ".join(issue_link(n) for n in sorted(pr_issues[number])))
        cells = [
            pr_link(number),
            _title(entry.get("title")),
            entry.get("state") or "-",
            status,
            "; ".join(notes) or "-",
        ]
        pr_rows.append("| " + " | ".join(cells) + " |")

    issue_rows = []
    issue_counts: dict[str, int] = dict.fromkeys(ISSUE_LEGEND, 0)
    for number in sorted(issues):
        entry = issues[number]
        status = issue_status(number, entry, trailers)
        issue_counts[status] += 1
        notes = []
        fixing = sorted(trailers.issue_prs.get(number, set()))
        if fixing:
            verb = "fixed" if status == "fixed" else "partly fixed"
            notes.append(f"{verb} by PR " + ", ".join(pr_link(n) for n in fixing))
        if number in trailers.issue_own:
            notes.append("own fix")
        linked = sorted(issue_links[number] - set(fixing))
        if linked:
            notes.append("PRs " + ", ".join(pr_link(n) for n in linked))
        if entry.get("reason"):
            notes.append(_text(entry["reason"]))
        cells = [
            issue_link(number),
            _title(entry.get("title")),
            entry.get("state") or "-",
            status,
            "; ".join(notes) or "-",
        ]
        issue_rows.append("| " + " | ".join(cells) + " |")

    own_rows = []
    for patch in trailers.own_changes:
        reasons = [FORK_ONLY_LABELS.get(value, value) for value in patch.fork_only]
        linked = ", ".join(issue_link(n) for n in _numbers(patch.issues)) or "-"
        own_rows.append(f"| {_title(patch.subject)} | {linked} | {_text('; '.join(reasons))} |")

    base = registry["base"]
    lines = [
        "# Upstream pull requests and issues",
        "",
        f"> Generated by `scripts/fork_registry.py` from `fork/decisions.toml` and the commit trailers."
        f" Do not edit by hand; run `{COMMAND} render`.",
        "",
        f"What this fork did with each pull request and issue of [{repo}]({url}). The patch queue sits on"
        f" upstream `main` at [`{base[:12]}`]({url}/commit/{base}); upstream state as of {registry['snapshot']}."
        " Policy: [FORK.md](../FORK.md).",
        "",
        "Scope: every pull request and issue open upstream at the snapshot, pull requests closed without"
        f" merge since {registry['closed_since']}, and every pull request and issue the fork's commits reference.",
        "",
        "## Summary",
        "",
        "| Status | Pull requests | Meaning |",
        "|---|---|---|",
        *(f"| {status} | {pr_counts[status]} | {meaning} |" for status, meaning in PR_LEGEND.items()),
        f"| total | {len(pr_rows)} | |",
        "",
        "| Status | Issues | Meaning |",
        "|---|---|---|",
        *(f"| {status} | {issue_counts[status]} | {meaning} |" for status, meaning in ISSUE_LEGEND.items()),
        f"| total | {len(issue_rows)} | |",
        "",
        "## Pull requests",
        "",
        "| PR | Title | Upstream | Status | Notes |",
        "|---|---|---|---|---|",
        *pr_rows,
        "",
        "## Issues",
        "",
        "| Issue | Title | Upstream | Status | Notes |",
        "|---|---|---|---|---|",
        *issue_rows,
        "",
        "## Own changes",
        "",
        "Fork commits without an upstream PR: fixes, security and dependency work."
        " Fork tooling, CI, policy, test-only and spec-only commits are not listed.",
        "",
        "| Subject | Issues | Reason |",
        "|---|---|---|",
        *own_rows,
    ]
    return "\n".join(lines) + "\n"


# --- sync ------------------------------------------------------------------------------


def gh_api(path: str) -> Any:
    result = subprocess.run(["gh", "api", path], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        message = f"gh api {path} failed: {result.stderr.strip()}"
        if re.search(r"HTTP 4(04|10)\b", result.stderr):
            raise NotFound(message)
        raise RegistryError(message)
    return json.loads(result.stdout)


def _pages(fetch: Fetch, path: str) -> Iterator[dict[str, Any]]:
    page = 1
    while True:
        items = fetch(f"{path}&per_page=100&page={page}")
        if not isinstance(items, list):
            raise RegistryError(f"unexpected payload for {path}")
        yield from items
        if len(items) < 100:
            return
        page += 1


def _item_state(item: dict[str, Any]) -> tuple[str, str]:
    """Kind and state of an issues-API item (PRs carry a `pull_request` object)."""
    pull = item.get("pull_request")
    if pull is not None:
        return "pr", "merged" if (pull.get("merged_at") or item.get("merged_at")) else str(item["state"])
    return "issue", str(item["state"])


def _clean_title(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("title") or "").split())


def sync_data(data: Data, trailers: Trailers, fetch: Fetch, snapshot: date, full: bool = False) -> list[str]:
    """Refresh data from upstream in place, reconcile it with the trailers and return the report."""
    registry = data["registry"]
    repo = f"repos/{registry['upstream']}"
    closed_since = registry["closed_since"].isoformat()
    since = registry["closed_since"] if full else registry["snapshot"]
    seen: dict[str, dict[int, tuple[str, str]]] = {"pr": {}, "issue": {}}
    wanted: dict[str, set[int]] = {"pr": set(), "issue": set()}
    report: list[str] = []

    for item in _pages(fetch, f"{repo}/issues?state=open"):
        kind, state = _item_state(item)
        seen[kind][item["number"]] = (_clean_title(item), state)
        wanted[kind].add(item["number"])
    for item in _pages(
        fetch, f"{repo}/issues?state=all&sort=updated&direction=desc&since={since.isoformat()}T00:00:00Z"
    ):
        kind, state = _item_state(item)
        seen[kind][item["number"]] = (_clean_title(item), state)
        closed_in_window = kind == "pr" and state == "closed" and (item.get("closed_at") or "")[:10] >= closed_since
        if state == "open" or closed_in_window:
            wanted[kind].add(item["number"])
    wanted["pr"].update(trailers.taken)
    wanted["pr"].update(e["by"] for e in data["pr"].values() if e.get("status") == "superseded" and e.get("by"))
    wanted["issue"].update(trailers.issue_prs)

    for kind in ("pr", "issue"):
        unknown = {n for n in wanted[kind] if n not in data[kind]}
        unknown |= {n for n, entry in data[kind].items() if entry.get("state") is None}
        # An entry recorded as open that neither listing returns was closed before the
        # listing window, deleted or transferred; refetch a bounded number per run.
        vanished = sorted(n for n, e in data[kind].items() if e.get("state") == "open" and n not in seen[kind])
        if len(vanished) > VANISHED_FETCH_LIMIT:
            report.append(
                f"{len(vanished)} open {kind} entries are missing from the listings; refreshed"
                f" {VANISHED_FETCH_LIMIT}, run sync again for the rest"
            )
        unknown |= set(vanished[:VANISHED_FETCH_LIMIT])
        for number in sorted(unknown - set(seen[kind])):
            try:
                item = fetch(f"{repo}/{'pulls' if kind == 'pr' else 'issues'}/{number}")
                home = f"https://github.com/{registry['upstream']}/"
                if not str(item.get("html_url") or home).startswith(home):
                    raise NotFound(f"moved to {item.get('html_url')}")
            except NotFound as exc:
                report.append(f"{kind} {number}: not found upstream, skipped ({exc})")
                entry = data[kind].get(number)
                if entry is not None and entry.get("state") in (None, "open"):
                    entry["state"] = "closed"
                wanted[kind].discard(number)
                continue
            if kind == "issue" and "pull_request" in item:
                commits = trailers.issue_commits.get(number)
                source = f"Upstream-Issue of commit {commits[0].subject!r}" if commits else f"[issue.{number}]"
                raise RegistryError(f"issue {number} ({source}) is a pull request upstream")
            state = ("merged" if item.get("merged_at") else str(item["state"])) if kind == "pr" else str(item["state"])
            seen[kind][number] = (_clean_title(item), state)

    added = {"pr": 0, "issue": 0}
    for kind in ("pr", "issue"):
        for number in sorted(wanted[kind] - set(data[kind])):
            title, state = seen[kind][number]
            data[kind][number] = {"title": title, "state": state, "status": "pending"}
            added[kind] += 1
        for number, entry in sorted(data[kind].items()):
            if number not in seen[kind]:
                continue
            title, state = seen[kind][number]
            old = entry.get("state")
            entry["title"] = title
            entry["state"] = state
            if old is not None and old != state:
                report.append(f"{kind} {number}: {old} -> {state}{_hint(kind, number, state, trailers)}")
    registry["snapshot"] = snapshot
    report.insert(0, f"added {added['pr']} PR(s) and {added['issue']} issue(s); snapshot {snapshot.isoformat()}")
    report += reconcile(data, trailers)
    return report


def reconcile(data: Data, trailers: Trailers) -> list[str]:
    """Fix entries the trailers no longer back, e.g. after a rebuild dropped a pick."""
    report = []
    for number, entry in sorted(data["pr"].items()):
        if number in trailers.taken:
            if entry.get("state") == "merged":
                report.append(f"pr {number}: taken PR merged upstream: drop on rebuild")
                if entry.get("status") in (None, "pending"):
                    entry["status"] = "merged-upstream"
            elif entry.get("status") is None:
                entry["status"] = "pending"
            continue
        if "rework" in entry:
            del entry["rework"]
            report.append(f"pr {number}: no longer taken, removed rework")
        if entry.get("status") in (None, "pending") and entry.get("state") == "merged":
            entry["status"] = "merged-upstream"
            report.append(f"pr {number}: merged upstream, status pending -> merged-upstream")
        elif entry.get("status") is None:
            entry["status"] = "pending"
    for number, entry in sorted(data["issue"].items()):
        if number not in trailers.issue_prs and entry.get("status") == "partial":
            entry["status"] = "pending"
            report.append(f"issue {number}: no longer referenced by trailers, status partial -> pending")
        elif entry.get("status") is None:
            entry["status"] = "pending"
    return report


def _hint(kind: str, number: int, state: str, trailers: Trailers) -> str:
    if kind == "pr":
        taken = number in trailers.taken
        if state == "merged":
            return " (taken: drop on rebuild)" if taken else ""
        if state == "closed":
            return " (taken: closed without merge, review)" if taken else " (closed without merge)"
        return " (reopened)"
    if state == "closed":
        if number in trailers.issue_prs:
            return " (fixed in the fork, closed upstream)"
        return " (candidate for fixed-upstream or not-applicable)"
    return " (reopened)"


# --- CLI -------------------------------------------------------------------------------


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning file={DOC_PATH}::{escaped}")


def _diff(old: str, new: str, path: Path) -> list[str]:
    return list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path} (fresh render)", lineterm="", n=1
        )
    )


def _parse(args: argparse.Namespace) -> tuple[Data, str, Trailers]:
    if not DATA_PATH.is_file():
        raise RegistryError(f"{DATA_PATH} is missing")
    data_text = DATA_PATH.read_text(encoding="utf-8")
    data, errors = parse_data(data_text)
    if errors:
        raise RegistryError("\n".join(f"{DATA_PATH}: {error}" for error in errors))
    return data, data_text, collect_trailers(args.base or data["registry"]["base"], args.head)


def _require_valid(data: Data, trailers: Trailers) -> None:
    errors = validate_data(data, trailers)
    if errors:
        raise RegistryError("\n".join(f"{DATA_PATH}: {error}" for error in errors))


def cmd_render(args: argparse.Namespace) -> int:
    data, _, trailers = _parse(args)
    _require_valid(data, trailers)
    document = render_document(data, trailers)
    DATA_PATH.write_text(serialize_data(data), encoding="utf-8")
    DOC_PATH.write_text(document, encoding="utf-8")
    print(f"wrote {DATA_PATH} and {DOC_PATH}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    data, data_text, trailers = _parse(args)
    _require_valid(data, trailers)
    failed = False
    canonical = serialize_data(data)
    if data_text != canonical:
        print("\n".join(_diff(data_text, canonical, DATA_PATH)))
        print(f"error: {DATA_PATH} is not canonical; run `{COMMAND} render` to rewrite it", file=sys.stderr)
        failed = True
    document = render_document(data, trailers)
    if not DOC_PATH.is_file():
        print(f"error: {DOC_PATH} is missing; run `{COMMAND} render` and commit it", file=sys.stderr)
        return 1
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(DOC_PATH)], capture_output=True, check=False)
    if tracked.returncode != 0:
        print(f"error: {DOC_PATH} is not tracked by git; add and commit it (is it ignored?)", file=sys.stderr)
        failed = True
    committed = DOC_PATH.read_text(encoding="utf-8")
    if committed != document:
        diff = _diff(committed, document, DOC_PATH)
        changed = [line for line in diff[2:] if line[:1] in "+-"]
        if args.strict:
            print("\n".join(diff))
            print(f"error: {DOC_PATH} is stale; run `{COMMAND} render`", file=sys.stderr)
            failed = True
        else:
            print("\n".join(changed[:20] + ([f"... {len(changed) - 20} more line(s)"] if len(changed) > 20 else [])))
            _warn(
                f"{DOC_PATH} is stale ({len(changed)} changed line(s), e.g. after new trailer commits);"
                f" run `{COMMAND} render`"
            )
    for signal in trailers.signals:
        if args.strict:
            print(f"error: trailer: {signal}", file=sys.stderr)
            failed = True
        else:
            _warn(f"trailer: {signal}")
    pending_prs, pending_issues = pending_entries(data, trailers)
    if args.strict and (pending_prs or pending_issues):
        sample = ", ".join([f"pr {n}" for n in pending_prs[:5]] + [f"issue {n}" for n in pending_issues[:5]])
        print(
            f"error: {len(pending_prs)} PR(s) and {len(pending_issues)} issue(s) are pending ({sample} ...)",
            file=sys.stderr,
        )
        failed = True
    print(
        f"fork registry: {len(data['pr'])} PR and {len(data['issue'])} issue entries,"
        f" {len(pending_prs)} PR(s) and {len(pending_issues)} issue(s) pending"
    )
    return 1 if failed else 0


def cmd_sync(args: argparse.Namespace) -> int:
    data, _, trailers = _parse(args)
    snapshot = date.fromisoformat(args.snapshot) if args.snapshot else datetime.now(UTC).date()
    report = sync_data(data, trailers, gh_api, snapshot, full=args.full)
    DATA_PATH.write_text(serialize_data(data), encoding="utf-8")
    print("\n".join(report))
    print(f"wrote {DATA_PATH}")
    _require_valid(data, trailers)
    print(f"run `{COMMAND} render` to refresh {DOC_PATH}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--base", help="override [registry].base")
    parser.add_argument("--head", default="HEAD", help="fork ref to read trailers from (default: HEAD)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("render", help="validate and rewrite the data file and the document")
    check = commands.add_parser("check", help="validate offline; a stale document is a warning")
    check.add_argument("--strict", action="store_true", help="also fail on a stale document, pending entries, trailers")
    sync = commands.add_parser("sync", help="refresh entries from upstream via gh api")
    sync.add_argument("--snapshot", help="snapshot date to record (default: today, UTC)")
    sync.add_argument("--full", action="store_true", help="refresh everything updated since closed_since")
    args = parser.parse_args(argv)
    os.chdir(args.root)
    try:
        return {"render": cmd_render, "check": cmd_check, "sync": cmd_sync}[args.command](args)
    except (RegistryError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
            raise
        print(f"error: {exc}" if isinstance(exc, RegistryError) else f"error: {exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
