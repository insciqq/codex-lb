from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts import fork_registry as registry

REPO_ROOT = Path(__file__).resolve().parents[2]
OPEN = "repos/o/r/issues?state=open&per_page=100&page={page}"
SINCE = "repos/o/r/issues?state=all&sort=updated&direction=desc&since={since}T00:00:00Z&per_page=100&page={page}"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, subject: str, *trailers: str) -> str:
    path = repo / f"change-{len(list(repo.glob('change-*')))}.txt"
    path.write_text(subject + "\n")
    git(repo, "add", path.name)
    git(repo, "commit", "-qm", subject + "\n\nBody.\n\n" + "\n".join(trailers))
    return git(repo, "rev-parse", "HEAD")


def data_text(base: str, entries: str = "") -> str:
    head = f'[registry]\nupstream = "owner/repo"\nbase = "{base}"\nsnapshot = 2026-01-02\nclosed_since = 2026-01-01\n'
    return head + ("\n" + entries.strip() + "\n" if entries.strip() else "")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "fork").mkdir()
    commit(repo, "chore: upstream base")
    commit(repo, "fix(proxy): taken upstream fix", "Upstream-PR: 10", "Upstream-PR-Head: abc", "Upstream-Issue: 5")
    commit(repo, "fix(db): own fix for an issue", "Upstream-PR: none", "Upstream-Issue: 6")
    commit(repo, "fix(app): own production fix", "Upstream-PR: none", "Fork-Only: production-fix")
    commit(repo, "test(app): follow-up test", "Upstream-PR: none", "Fork-Only: production-fix")
    commit(repo, "docs(fork): tooling", "Upstream-PR: none", "Fork-Only: tooling")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return repo


def base_of(repo: Path) -> str:
    return git(repo, "rev-list", "--max-parents=0", "HEAD")


def write_data(repo: Path, entries: str = "") -> None:
    (repo / "fork/decisions.toml").write_text(data_text(base_of(repo), entries))


def run(repo: Path, *args: str) -> int:
    return registry.main(["--root", str(repo), *args])


def render(repo: Path) -> str:
    assert run(repo, "render") == 0
    git(repo, "add", "fork/REGISTRY.md")
    return (repo / "fork/REGISTRY.md").read_text()


ENTRIES = """
[pr.10]
title = "fix: upstream title | with pipe"
state = "open"
status = "pending"
rework = "Rebased onto the new retry path."

[pr.11]
title = "feat: something big"
state = "open"
status = "out-of-scope"

[pr.12]
title = "fix: risky"
state = "closed"
status = "rejected"
reason = "Breaks *shared* connections."
issues = [7]

[pr.13]
title = "fix: older attempt"
state = "open"
status = "superseded"
by = 10

[issue.5]
title = "Bug five"
state = "open"
status = "pending"

[issue.7]
title = "Bug seven"
state = "open"
status = "pending"
"""


def test_render_derives_statuses_from_trailers(repo: Path) -> None:
    write_data(repo, ENTRIES)
    doc = render(repo)

    assert "| [10](https://github.com/owner/repo/pull/10) | fix: upstream title \\| with pipe | open |" in doc
    assert "| taken with changes | changes: Rebased onto the new retry path.; issues [5]" in doc
    assert "| superseded | superseded by [10](https://github.com/owner/repo/pull/10) |" in doc
    # Authored reasons keep their markdown.
    assert "| rejected | Breaks *shared* connections.; issues [7](https://github.com/owner/repo/issues/7) |" in doc
    assert "| fixed | fixed by PR [10](https://github.com/owner/repo/pull/10) |" in doc
    # Issue 6 has no data entry but is referenced by an own commit.
    assert "| [6](https://github.com/owner/repo/issues/6) | - | - | fixed | own fix |" in doc
    assert "| pending | PRs [12](https://github.com/owner/repo/pull/12) |" in doc
    own = doc.split("## Own changes", 1)[1]
    assert "fix(db): own fix for an issue | [6]" in own
    assert "fix(app): own production fix | - | bug found in production or review |" in own
    assert "follow-up test" not in own
    assert "docs(fork): tooling" not in own
    assert run(repo, "check") == 0


def test_taken_pr_without_entry_uses_commit_subject(repo: Path) -> None:
    write_data(repo)
    doc = render(repo)
    assert "| [10](https://github.com/owner/repo/pull/10) | fix(proxy): taken upstream fix | - | taken |" in doc


def test_titles_and_subjects_are_not_live_markdown(repo: Path) -> None:
    # Each escape was checked against cmark-gfm: the cell renders as the literal text.
    commit(repo, "fix(api): handle `<think>` & [x](y) *bold*", "Upstream-PR: none", "Fork-Only: production-fix")
    title = "![img](http://x/y.png) _a_ \\\\ <b> ~~s~~ www.example.org a@example.org"
    write_data(repo, f'[pr.30]\ntitle = "{title}"\nstate = "open"\nstatus = "pending"\n')
    doc = render(repo)
    expected = (
        "| \\!\\[img\\](http\\://x/y.png) \\_a\\_ \\\\ \\<b> \\~\\~s\\~\\~ www\\.example.org a@&#8203;example.org |"
    )
    assert expected in doc
    assert "| fix(api): handle \\`\\<think>\\` \\& \\[x\\](y) \\*bold\\* | - |" in doc
    # Generated links stay live.
    assert "| [30](https://github.com/owner/repo/pull/30) |" in doc


def test_check_fails_when_document_is_missing_or_untracked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_data(repo)
    assert run(repo, "check") == 1
    assert "fork/REGISTRY.md is missing" in capsys.readouterr().err
    assert run(repo, "render") == 0
    assert run(repo, "check") == 1
    assert "fork/REGISTRY.md is not tracked by git" in capsys.readouterr().err
    git(repo, "add", "fork/REGISTRY.md")
    assert run(repo, "check") == 0


def test_missing_data_file_is_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(repo, "check") == 1
    assert "error: fork/decisions.toml is missing" in capsys.readouterr().err


def test_stale_document_warns_by_default_and_fails_strict(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_data(repo)
    render(repo)
    commit(repo, "fix(proxy): another taken fix", "Upstream-PR: 20", "Upstream-PR-Head: def")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    capsys.readouterr()

    assert run(repo, "check") == 0
    out = capsys.readouterr()
    assert "::warning file=fork/REGISTRY.md::fork/REGISTRY.md is stale" in out.out
    assert "+| [20](https://github.com/owner/repo/pull/20) | fix(proxy): another taken fix" in out.out

    assert run(repo, "check", "--strict") == 1
    assert "is stale" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("trailers", "signal"),
    [
        (("Upstream-PR: 20",), "Upstream-PR 20 without Upstream-PR-Head"),
        (("Fork-Only: tooling",), "missing Upstream-PR trailer"),
        (("Upstream-PR: ²", "Upstream-PR-Head: abc"), "invalid Upstream-PR value '²'"),
    ],
)
def test_trailer_signals_warn_by_default_and_fail_strict(
    repo: Path, capsys: pytest.CaptureFixture[str], trailers: tuple[str, ...], signal: str
) -> None:
    commit(repo, "fix(proxy): odd trailers", *trailers)
    write_data(repo)
    render(repo)
    capsys.readouterr()

    assert run(repo, "check") == 0
    err = capsys.readouterr().err
    assert "warning: trailer: " in err
    assert signal in err
    assert run(repo, "check", "--strict") == 1
    assert signal in capsys.readouterr().err


def test_strict_fails_on_pending_entries(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_data(repo, '[pr.30]\ntitle = "x"\nstate = "open"\nstatus = "pending"\n')
    render(repo)
    assert run(repo, "check") == 0
    capsys.readouterr()
    assert run(repo, "check", "--strict") == 1
    assert "1 PR(s) and 0 issue(s) are pending" in capsys.readouterr().err


def test_check_fails_on_non_canonical_data(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_data(repo, '[pr.30]\nstatus = "rejected"\nreason = "No."\nstate = "open"\n')
    (repo / "fork/REGISTRY.md").write_text("")
    git(repo, "add", "fork/REGISTRY.md")
    assert run(repo, "check") == 1
    assert "is not canonical" in capsys.readouterr().err
    render(repo)
    assert run(repo, "check") == 0
    canonical = '[pr.30]\nstate = "open"\nstatus = "rejected"\nreason = "No."\n'
    assert canonical in (repo / "fork/decisions.toml").read_text()


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ('[pr.10]\nstatus = "rejected"\nreason = "x"\n', "taken by commit 'fix(proxy): taken upstream fix'"),
        ("[pr.10]\n", 'status missing (use status = "pending")'),
        (
            '[pr.11]\nstatus = "pending"\nrework = "x"\n',
            "rework is only valid for a taken PR (was the pick dropped? run sync)",
        ),
        ('[pr.11]\nstatus = "rejected"\n', "status rejected requires reason"),
        ('[pr.11]\nstatus = "not-applicable"\n', "status not-applicable requires reason"),
        ('[pr.11]\nstatus = "superseded"\nby = 99\n', "by = 99 must name another PR entry or a taken PR (run sync"),
        ('[pr.11]\nstatus = "superseded"\nby = 11\n', "a PR cannot supersede itself"),
        ('[pr.11]\nstatus = "pending"\nby = 10\n', "by is only valid for superseded"),
        ('[pr.11]\nstatus = "superseded"\n', "superseded requires by"),
        ('[pr.11]\nstatus = "own-fix"\n', "own-fix requires issues or reason"),
        ('[pr.11]\nstatus = "merged-upstream"\nstate = "open"\n', "merged-upstream requires state"),
        ('[pr.11]\nstatus = "pending"\nstate = "draft"\n', "state must be one of open, closed, merged"),
        ('[issue.8]\nstatus = "pending"\nstate = "merged"\n', "state must be one of open, closed"),
        ('[pr.11]\nstatus = "taken"\n', "unknown status 'taken'"),
        ('[pr.11]\ntitle = "x"\n', "status missing"),
        ('[pr.11]\nstatus = "pending"\nissues = ["7"]\n', "issues must be a list of numbers"),
        ('[pr.11]\nstatus = "pending"\nnote = "x"\n', "unknown key 'note'"),
        ('[pr.11]\nstatus = "rejected"\nreason = "two\\nlines"\n', "reason must be a single line"),
        ('[other.1]\nstatus = "pending"\n', "unknown table [other]"),
        ('[issue.5]\nstatus = "fixed-upstream"\n', "fixed by trailers; data may only set pending or partial"),
        (
            '[issue.8]\nstatus = "partial"\nreason = "x"\n',
            "partial is only valid for an issue the trailers reference (run sync)",
        ),
        ('[issue.5]\nstatus = "partial"\n', "status partial requires reason"),
        ('[issue.8]\nstatus = "not-fixed"\n', "status not-fixed requires reason"),
    ],
)
def test_validation_rejects_invalid_entries(
    repo: Path, capsys: pytest.CaptureFixture[str], entries: str, message: str
) -> None:
    write_data(repo, entries)
    assert run(repo, "check") == 1
    assert message in capsys.readouterr().err


def test_superseded_cycle_is_rejected(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_data(
        repo,
        '[pr.1]\nstatus = "superseded"\nby = 2\n\n[pr.2]\nstatus = "superseded"\nby = 3\n\n'
        '[pr.3]\nstatus = "superseded"\nby = 2\n',
    )
    assert run(repo, "check") == 1
    err = capsys.readouterr().err
    assert "[pr.2]: superseded chain loops back to 2" in err
    assert "[pr.3]: superseded chain loops back to 3" in err
    assert "[pr.1]: superseded chain" not in err


def test_base_must_be_an_ancestor(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_data(repo)
    assert run(repo, "--base", "a" * 40, "check") == 1
    assert "is not an ancestor" in capsys.readouterr().err


def test_serializer_round_trips_escapes() -> None:
    data: dict[str, Any] = {
        "registry": {
            "upstream": "o/r",
            "base": "a" * 40,
            "snapshot": date(2026, 1, 2),
            "closed_since": date(2026, 1, 1),
        },
        "pr": {
            2: {
                "title": 'quote " back \\ tab \t bell \x07 del \x7f ü',
                "state": "open",
                "status": "pending",
                "issues": [3, 1, 3],
            }
        },
        "issue": {},
    }
    text = registry.serialize_data(data)
    assert "\\u0007" in text
    assert "\\u007f" in text
    parsed, errors = registry.parse_data(text)
    assert errors == []
    assert parsed["pr"][2]["title"] == data["pr"][2]["title"]
    assert parsed["pr"][2]["issues"] == [1, 3]
    assert registry.serialize_data(parsed) == text


def _item(number: int, state: str = "open", *, pr: bool = True, merged: bool = False, closed: str | None = None):
    item: dict[str, Any] = {"number": number, "title": f"Item {number}", "state": state, "closed_at": closed}
    if pr:
        item["pull_request"] = {"merged_at": "2026-01-05T00:00:00Z" if merged else None}
    return item


def _data(**prs: Any) -> dict[str, Any]:
    return {
        "registry": {
            "upstream": "o/r",
            "base": "a" * 40,
            "snapshot": date(2026, 1, 1),
            "closed_since": date(2026, 1, 1),
        },
        "pr": {int(k[1:]): v for k, v in prs.items()},
        "issue": {},
    }


def _fetcher(responses: dict[str, Any], fetched: list[str]):
    def fetch(path: str) -> Any:
        fetched.append(path)
        response = responses[path]
        if isinstance(response, Exception):
            raise response
        return response

    return fetch


def test_sync_adds_missing_entries_and_refreshes_changed_ones() -> None:
    responses = {
        OPEN.format(page=1): [_item(n) for n in range(100, 199)] + [_item(400, pr=False)],
        OPEN.format(page=2): [_item(201), {"number": 401, "title": "multi\r\nline\ttitle", "state": "open"}],
        SINCE.format(since="2026-01-01", page=1): [
            _item(300, "closed", closed="2026-01-03T00:00:00Z"),
            _item(301, "closed", merged=True, closed="2026-01-02T00:00:00Z"),
            # Updated after the snapshot but closed before closed_since: not in scope.
            _item(302, "closed", closed="2025-12-01T00:00:00Z"),
            # Recorded as closed already; its title changed upstream.
            {**_item(303, "closed", closed="2025-11-01T00:00:00Z"), "title": "Renamed"},
            # Opened after the open listing was read: only the second listing has it.
            _item(402, pr=False),
        ],
        "repos/o/r/pulls/10": {"number": 10, "title": "PR 10", "state": "closed", "merged_at": "2026-01-05T00:00:00Z"},
        "repos/o/r/issues/5": {"number": 5, "title": "Issue 5", "state": "closed"},
    }
    fetched: list[str] = []
    data = _data(
        p301={"title": "old", "state": "open", "status": "pending"},
        p303={"title": "old", "state": "closed", "status": "rejected", "reason": "x"},
    )
    patch = registry.Patch("abc", "fix: taken", ["10"], ["h"], ["5"], [])
    trailers = registry.Trailers([patch], taken={10: [patch]}, issue_prs={5: {10}})

    report = registry.sync_data(data, trailers, _fetcher(responses, fetched), date(2026, 1, 7))

    assert set(data["pr"]) == set(range(100, 199)) | {201, 300, 301, 303, 10}
    assert data["pr"][300] == {"title": "Item 300", "state": "closed", "status": "pending"}
    assert data["pr"][10] == {"title": "PR 10", "state": "merged", "status": "merged-upstream"}
    assert data["pr"][301] == {"title": "Item 301", "state": "merged", "status": "merged-upstream"}
    assert data["pr"][303]["title"] == "Renamed"
    assert set(data["issue"]) == {400, 401, 402, 5}
    assert data["issue"][401]["title"] == "multi line title"
    assert data["registry"]["snapshot"] == date(2026, 1, 7)
    assert "pr 301: open -> merged" in report
    assert "pr 301: merged upstream, status pending -> merged-upstream" in report
    assert "pr 10: taken PR merged upstream: drop on rebuild" in report
    assert len([path for path in fetched if "issues?" in path]) == 3
    assert "repos/o/r/pulls/302" not in fetched


def test_sync_rejects_a_trailer_issue_that_is_a_pull_request() -> None:
    responses = {
        OPEN.format(page=1): [],
        SINCE.format(since="2026-01-01", page=1): [],
        "repos/o/r/issues/5": _item(5),
    }
    patch = registry.Patch("abc", "fix: own", ["none"], [], ["5"], [])
    trailers = registry.Trailers([patch], issue_prs={5: set()}, issue_commits={5: [patch]})
    with pytest.raises(registry.RegistryError, match="issue 5 \\(Upstream-Issue of commit 'fix: own'\\) is a pull"):
        registry.sync_data(_data(), trailers, _fetcher(responses, []), date(2026, 1, 7))


def test_sync_skips_entries_gone_upstream() -> None:
    responses = {
        OPEN.format(page=1): [],
        SINCE.format(since="2026-01-01", page=1): [],
        "repos/o/r/pulls/7": registry.NotFound("HTTP 404"),
        "repos/o/r/pulls/8": registry.NotFound("HTTP 410"),
        "repos/o/r/pulls/9": {**_item(9), "html_url": "https://github.com/other/repo/pull/9"},
        "repos/o/r/pulls/11": {**_item(11, "closed"), "html_url": "https://github.com/o/r/pull/11"},
    }
    patch = registry.Patch("abc", "fix: taken", ["8"], ["h"], [], [])
    trailers = registry.Trailers([patch], taken={8: [patch]})
    # 7 has never been synced; 9 (transferred) and 11 (closed before the window) were open.
    data = _data(
        p7={"status": "pending"},
        p9={"title": "Item 9", "state": "open", "status": "pending"},
        p11={"title": "Item 11", "state": "open", "status": "pending"},
    )

    report = registry.sync_data(data, trailers, _fetcher(responses, []), date(2026, 1, 7))

    assert data["pr"] == {
        7: {"status": "pending", "state": "closed"},
        9: {"title": "Item 9", "state": "closed", "status": "pending"},
        11: {"title": "Item 11", "state": "closed", "status": "pending"},
    }
    assert any(line.startswith("pr 7: not found upstream") for line in report)
    assert any(line.startswith("pr 8: not found upstream") for line in report)
    assert any(
        line.startswith("pr 9: not found upstream, skipped (moved to https://github.com/other") for line in report
    )
    assert "pr 11: open -> closed (closed without merge)" in report


def test_sync_caps_refetches_of_vanished_open_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "VANISHED_FETCH_LIMIT", 2)
    responses: dict[str, Any] = {OPEN.format(page=1): [], SINCE.format(since="2026-01-01", page=1): []}
    responses.update({f"repos/o/r/pulls/{n}": _item(n, "closed") for n in (1, 2)})
    data = _data(**{f"p{n}": {"title": "x", "state": "open", "status": "pending"} for n in (1, 2, 3)})

    report = registry.sync_data(data, registry.Trailers([]), _fetcher(responses, []), date(2026, 1, 7))

    assert [data["pr"][n]["state"] for n in (1, 2, 3)] == ["closed", "closed", "open"]
    assert "3 open pr entries are missing from the listings; refreshed 2, run sync again for the rest" in report


def test_taken_pr_merged_upstream_renders_as_taken_with_drop_note(repo: Path) -> None:
    write_data(repo, '[pr.10]\ntitle = "fix: upstream"\nstate = "merged"\nstatus = "merged-upstream"\n')
    doc = render(repo)
    assert "| fix: upstream | merged | taken | merged upstream; drop on rebuild; issues [5]" in doc
    assert run(repo, "check") == 0


def test_taken_pr_merged_upstream_is_promoted_by_reconcile() -> None:
    patch = registry.Patch("abc", "fix: taken", ["10"], ["h"], [], [])
    trailers = registry.Trailers([patch], taken={10: [patch]})
    data = _data(p10={"title": "x", "state": "merged", "status": "pending"})
    report = registry.reconcile(data, trailers)
    assert data["pr"][10]["status"] == "merged-upstream"
    assert report == ["pr 10: taken PR merged upstream: drop on rebuild"]
    assert registry.validate_data(data, trailers) == []
    data["pr"][10]["state"] = "open"
    assert registry.validate_data(data, trailers) == ['[pr.10]: status merged-upstream requires state = "merged"']


REBUILD_ENTRIES = """
[pr.10]
title = "fix: upstream title"
state = "open"
status = "pending"
rework = "Kept the old timeout."

[pr.13]
title = "fix: older attempt"
state = "open"
status = "superseded"
by = 10

[issue.5]
title = "Bug five"
state = "open"
status = "partial"
reason = "Half done."
"""


def test_rebuild_that_drops_a_merged_pick_is_reconciled_by_sync(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_data(repo, REBUILD_ENTRIES)
    assert "| partial | partly fixed by PR [10](https://github.com/owner/repo/pull/10); Half done. |" in render(repo)
    assert run(repo, "check") == 0

    # Rebuild: upstream merged PR 10, so the new queue drops its pick.
    picks = git(repo, "rev-list", "--reverse", f"{base_of(repo)}..HEAD").split()
    git(repo, "reset", "-q")  # keep the uncommitted registry files out of the picks
    git(repo, "checkout", "-q", "-B", "rebuilt", base_of(repo))
    git(repo, "cherry-pick", *picks[1:])
    capsys.readouterr()
    assert run(repo, "check") == 1
    err = capsys.readouterr().err
    assert "[pr.10]: rework is only valid for a taken PR (was the pick dropped? run sync)" in err
    assert "[issue.5]: partial is only valid for an issue the trailers reference (run sync)" in err

    responses = {
        "repos/owner/repo/issues?state=open&per_page=100&page=1": [],
        "repos/owner/repo/issues?state=all&sort=updated&direction=desc&since=2026-01-02T00:00:00Z"
        "&per_page=100&page=1": [{**_item(10, "closed", merged=True), "title": "fix: upstream title"}],
        "repos/owner/repo/issues/6": {"number": 6, "title": "Bug six", "state": "open"},
        "repos/owner/repo/pulls/13": {"number": 13, "title": "fix: older attempt", "state": "open"},
        "repos/owner/repo/issues/5": {"number": 5, "title": "Bug five", "state": "open"},
    }
    monkeypatch.setattr(registry, "gh_api", _fetcher(responses, []))
    assert run(repo, "sync", "--snapshot", "2026-01-08") == 0
    out = capsys.readouterr().out
    assert "pr 10: no longer taken, removed rework" in out
    assert "pr 10: merged upstream, status pending -> merged-upstream" in out
    assert "issue 5: no longer referenced by trailers, status partial -> pending" in out

    doc = render(repo)
    assert "| [10](https://github.com/owner/repo/pull/10) | fix: upstream title | merged | merged-upstream |" in doc
    assert "| superseded | superseded by [10]" in doc
    assert run(repo, "check") == 0


def test_committed_registry_of_this_repository_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    data_path = REPO_ROOT / "fork/decisions.toml"
    if not data_path.is_file():
        pytest.skip("no fork registry in this checkout")
    data, _ = registry.parse_data(data_path.read_text(encoding="utf-8"))
    base = data["registry"]["base"]
    present = subprocess.run(["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{base}^{{commit}}"], check=False)
    if present.returncode != 0:
        pytest.skip("shallow checkout: registry base commit not available")
    monkeypatch.chdir(REPO_ROOT)
    assert registry.main(["--root", str(REPO_ROOT), "check"]) == 0


DEFERRED_ENTRIES = """
[pr.30]
title = "fix: after the release"
state = "open"
status = "deferred"
reason = "Needs a recovery path first."

[issue.40]
title = "Bug forty"
state = "open"
status = "deferred"
reason = "Being redesigned."
"""


def test_deferred_entries_render_and_do_not_fail_strict(repo: Path) -> None:
    write_data(repo, DEFERRED_ENTRIES)
    doc = render(repo)

    pr_summary, issue_summary = doc.split("| Status | Issues | Meaning |", 1)
    assert "| deferred | 1 | decided, scheduled after the first release |" in pr_summary
    assert "| deferred | 1 | decided, scheduled after the first release |" in issue_summary
    assert "| fix: after the release | open | deferred | Needs a recovery path first. |" in doc
    assert "| Bug forty | open | deferred | Being redesigned. |" in doc
    assert run(repo, "check", "--strict") == 0


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            '[pr.10]\nstatus = "deferred"\nreason = "x"\n',
            "data may only set pending or merged-upstream, not 'deferred'",
        ),
        ('[issue.5]\nstatus = "deferred"\nreason = "x"\n', "data may only set pending or partial, not 'deferred'"),
        ('[pr.30]\nstatus = "deferred"\n', "status deferred requires reason"),
        ('[issue.40]\nstatus = "deferred"\n', "status deferred requires reason"),
    ],
)
def test_deferred_contradicting_trailers_or_without_reason_is_rejected(
    repo: Path, capsys: pytest.CaptureFixture[str], entries: str, message: str
) -> None:
    write_data(repo, entries)
    assert run(repo, "check") == 1
    assert message in capsys.readouterr().err


def test_sync_keeps_deferred_entries() -> None:
    responses = {
        OPEN.format(page=1): [_item(41, pr=False)],
        SINCE.format(since="2026-01-01", page=1): [_item(30, "closed", merged=True, closed="2026-01-03T00:00:00Z")],
    }
    data = _data(p30={"title": "x", "state": "open", "status": "deferred", "reason": "Later."})
    data["issue"][41] = {"title": "y", "state": "open", "status": "deferred", "reason": "Later."}

    registry.sync_data(data, registry.Trailers([]), _fetcher(responses, []), date(2026, 1, 7))

    assert data["pr"][30] == {"title": "Item 30", "state": "merged", "status": "deferred", "reason": "Later."}
    assert data["issue"][41] == {"title": "Item 41", "state": "open", "status": "deferred", "reason": "Later."}
