"""Tests for the gh layer: slug parsing, run states, check folding, failure modes."""

from __future__ import annotations

import pytest

from task_viewer import github
from task_viewer.github import Snapshot, fetch, slug_of


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/arnovich/gimle-mimir.git", "arnovich/gimle-mimir"),
        ("https://github.com/arnovich/gimle-mimir", "arnovich/gimle-mimir"),
        ("git@github.com:gimlelabs/gimle-hugin.git", "gimlelabs/gimle-hugin"),
        ("ssh://git@github.com/a/b.git", "a/b"),
        ("https://gitlab.com/a/b.git", None),
        ("/tmp/origins/alpha.git", None),
        ("https://github.com/a/b/c", None),
    ],
)
def test_the_slug_comes_from_a_github_url_only(url: str, slug: str | None) -> None:
    assert slug_of(url) == slug


RUNS = [
    {"databaseId": 1, "workflowName": "CPU tests", "displayTitle": "task 003: plan", "status": "in_progress",
     "conclusion": "", "headBranch": "task/003_busy", "event": "push", "createdAt": "2026-09-24T10:00:00Z",
     "updatedAt": "2026-09-24T10:01:00Z", "url": "https://github.com/x/y/actions/runs/1"},
    {"databaseId": 2, "workflowName": "CPU tests", "displayTitle": "Seed", "status": "queued", "conclusion": "",
     "headBranch": "main", "event": "push", "createdAt": "2026-09-24T10:02:00Z", "updatedAt": "2026-09-24T10:02:00Z",
     "url": "https://github.com/x/y/actions/runs/2"},
    {"databaseId": 3, "workflowName": "Lint", "displayTitle": "Seed", "status": "completed", "conclusion": "failure",
     "headBranch": "main", "event": "push", "createdAt": "2026-09-24T09:00:00Z", "updatedAt": "2026-09-24T09:05:00Z",
     "url": "https://github.com/x/y/actions/runs/3"},
    {"databaseId": 4, "workflowName": "Lint", "displayTitle": "Old", "status": "completed", "conclusion": "cancelled",
     "headBranch": "main", "event": "push", "createdAt": "2026-09-24T08:00:00Z", "updatedAt": "2026-09-24T08:05:00Z",
     "url": "https://github.com/x/y/actions/runs/4"},
    {"databaseId": 5, "workflowName": "CPU tests", "displayTitle": "Earlier", "status": "completed", "conclusion": "success",
     "headBranch": "main", "event": "push", "createdAt": "2026-09-24T07:00:00Z", "updatedAt": "2026-09-24T07:05:00Z",
     "url": "https://github.com/x/y/actions/runs/5"},
]
PULLS = [
    {"number": 7, "title": "task 003: busy work", "headRefName": "task/003_busy", "url": "https://github.com/x/y/pull/7",
     "isDraft": False, "createdAt": "2026-09-24T09:30:00Z", "updatedAt": "2026-09-24T10:01:00Z",
     "author": {"login": "claude"}, "reviewDecision": "", "mergeable": "MERGEABLE",
     "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"state": "PENDING"}]},
    {"number": 8, "title": "Something unrelated", "headRefName": "feat/other", "url": "https://github.com/x/y/pull/8",
     "isDraft": True, "createdAt": "2026-09-23T09:30:00Z", "updatedAt": "2026-09-23T10:01:00Z",
     "author": {"login": "erikarne"}, "reviewDecision": "CHANGES_REQUESTED", "mergeable": "CONFLICTING",
     "statusCheckRollup": [{"conclusion": "FAILURE"}, {"conclusion": "SUCCESS"}]},
    {"number": 9, "title": "No checks", "headRefName": "feat/none", "url": "https://github.com/x/y/pull/9",
     "isDraft": False, "createdAt": None, "updatedAt": None, "author": None, "reviewDecision": "APPROVED",
     "mergeable": "", "statusCheckRollup": []},
]


def fake_gh(answers: dict[str, tuple[list, str]]):
    """A stand-in for the gh call: keyed by the subcommand ('run' or 'pr')."""
    def _gh_json(*args: str) -> tuple[list, str]:
        return answers.get(args[0], ([], f"no answer for {args[0]}"))
    return _gh_json


def test_fetch_folds_run_states_and_check_rollups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh_json", fake_gh({"run": (RUNS, ""), "pr": (PULLS, "")}))
    snapshot = fetch("x/y")
    assert snapshot.ok and snapshot.checked is not None
    assert [r.state for r in snapshot.runs] == ["running", "queued", "failed", "cancelled", "passed"]
    assert [r.decided for r in snapshot.runs] == [False, False, True, False, True]
    assert snapshot.runs[0].live and not snapshot.runs[2].live
    assert snapshot.runs[0].updated is not None and snapshot.runs[0].updated.tzinfo is not None
    busy, other, none = snapshot.pulls
    assert (busy.checks, busy.checks_done, busy.checks_total) == ("pending", 1, 2)
    assert (other.checks, other.draft, other.review, other.mergeable) == ("failing", True, "CHANGES_REQUESTED", "CONFLICTING")
    assert (none.checks, none.author, none.updated) == ("none", "", None)


def test_fetch_keeps_what_it_could_get_and_says_what_it_could_not(monkeypatch: pytest.MonkeyPatch) -> None:
    # Actions disabled: no runs, but the pull requests are still there.
    monkeypatch.setattr(github, "_gh_json", fake_gh({"run": ([], "HTTP 404: Not Found"), "pr": (PULLS, "")}))
    snapshot = fetch("x/y")
    assert not snapshot.ok and not snapshot.runs_ok and snapshot.pulls_ok
    assert snapshot.runs_error == "HTTP 404: Not Found" and len(snapshot.pulls) == 3
    assert snapshot.error == "runs: HTTP 404: Not Found"
    monkeypatch.setattr(github, "_gh_json", fake_gh({"run": (RUNS, ""), "pr": ([], "boom")}))
    snapshot = fetch("x/y")
    assert snapshot.runs_ok and snapshot.pulls_error == "boom" and len(snapshot.runs) == 5


def test_the_default_branch_is_asked_about_separately_and_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    main_only = [dict(RUNS[4], databaseId=99, displayTitle="An old main run")]

    def _gh_json(*args: str):
        calls.append(args)
        if args[0] == "pr":
            return PULLS, ""
        return (main_only if "--branch" in args else RUNS), ""

    monkeypatch.setattr(github, "_gh_json", _gh_json)
    snapshot = fetch("x/y", "main")
    assert [c[:2] for c in calls] == [("run", "list"), ("run", "list"), ("pr", "list")]
    assert "--branch" in calls[1] and "main" in calls[1]
    assert all("github.com/x/y" in c for c in calls)  # the host is pinned
    assert [r.id for r in snapshot.runs] == [1, 2, 3, 4, 5, 99]  # merged, no duplicates


def test_fetch_never_raises_on_a_strange_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    odd = [{"databaseId": "abc", "url": "javascript:alert(1)", "status": "completed", "conclusion": "success"}, "not a dict", None]
    monkeypatch.setattr(github, "_gh_json", fake_gh({"run": (odd, ""), "pr": ([{"number": None, "url": "http://insecure/1"}], "")}))
    snapshot = fetch("x/y")
    assert snapshot.ok
    assert snapshot.runs[0].id == 0 and snapshot.runs[0].url == ""  # only https links are kept
    assert snapshot.pulls[0].number == 0 and snapshot.pulls[0].url == ""

    def explode(*args):
        raise RuntimeError("gh changed its output")

    monkeypatch.setattr(github, "_gh_json", explode)
    snapshot = fetch("x/y")
    assert not snapshot.ok and "gh changed its output" in snapshot.runs_error


def test_a_missing_gh_is_an_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing)
    snapshot = fetch("x/y")
    assert snapshot.runs_error == "gh is not installed" and snapshot.pulls_error == "gh is not installed"
    assert isinstance(Snapshot(), Snapshot) and Snapshot().ok
