"""Tests for the pull-request view: what `gh` reports, and how it is worded.

`gh` itself is stubbed — a fake on PATH that prints canned JSON — so nothing
here touches the network or a real repository.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from task_viewer.pull_requests import (
    PullRequest,
    checks_summary,
    comment,
    load_pull_requests,
    merge,
)


def _fake_gh(tmp_path: Path, payload: object, exit_code: int = 0) -> Path:
    """A `gh` on PATH that echoes ``payload`` and records how it was called."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"pathlib.Path({str(tmp_path / 'calls.log')!r}).open('a').write("
        "' '.join(sys.argv[1:]) + chr(10))\n"
        f"sys.stdout.write({json.dumps(json.dumps(payload))})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return binary


@pytest.fixture
def gh_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a fake gh; returns a reader for the commands it received."""

    def install(payload: object, exit_code: int = 0):
        _fake_gh(tmp_path, payload, exit_code)
        monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
        log = tmp_path / "calls.log"
        return lambda: log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    return install


_PR = {
    "number": 453,
    "title": "Task 140: derivative-augmented token features",
    "body": "## Summary\nAdds derivative features.\n",
    "url": "https://github.com/arnovich/gimle-mimir/pull/453",
    "headRefName": "task/140-derivative-augmented-token-features",
    "isDraft": False,
    "mergeable": "MERGEABLE",
    "state": "OPEN",
    "reviewDecision": "",
    "statusCheckRollup": [
        {"name": "test", "conclusion": "SUCCESS"},
        {"name": "lint", "conclusion": "FAILURE"},
    ],
    "comments": [
        {"author": {"login": "erikarne"}, "body": "Add the benchmark."},
        {"author": {"login": "cloudflare-workers-and-pages"}, "body": "Deploying…"},
    ],
}


def test_pull_requests_are_keyed_by_branch(tmp_path: Path, gh_calls) -> None:
    """A worktree knows its branch, not its PR number."""
    gh_calls([_PR])
    found = load_pull_requests(tmp_path)
    assert list(found) == ["task/140-derivative-augmented-token-features"]
    assert found["task/140-derivative-augmented-token-features"].number == 453


def test_a_repo_without_gh_or_a_remote_yields_nothing(tmp_path: Path, gh_calls) -> None:
    gh_calls([], exit_code=1)
    assert load_pull_requests(tmp_path) == {}


def test_check_state_counts_what_failed(tmp_path: Path, gh_calls) -> None:
    gh_calls([_PR])
    pr = load_pull_requests(tmp_path)["task/140-derivative-augmented-token-features"]
    assert pr.checks_passed == 1
    assert pr.checks_total == 2
    assert pr.checks_failing is True


def test_a_pr_with_no_checks_is_not_reported_as_failing(tmp_path: Path, gh_calls) -> None:
    gh_calls([{**_PR, "statusCheckRollup": []}])
    pr = load_pull_requests(tmp_path)["task/140-derivative-augmented-token-features"]
    assert pr.checks_total == 0
    assert pr.checks_failing is False


def test_a_pending_check_is_not_a_failure(tmp_path: Path, gh_calls) -> None:
    """A run still in flight has no conclusion; that is not a red check."""
    gh_calls([{**_PR, "statusCheckRollup": [{"name": "test", "conclusion": ""}]}])
    pr = load_pull_requests(tmp_path)["task/140-derivative-augmented-token-features"]
    assert pr.checks_failing is False
    assert pr.checks_pending == 1


def test_bot_comments_are_separated_from_human_ones(tmp_path: Path, gh_calls) -> None:
    """Deploy-preview bots would otherwise drown the review you care about."""
    gh_calls([_PR])
    pr = load_pull_requests(tmp_path)["task/140-derivative-augmented-token-features"]
    assert [c.author for c in pr.human_comments] == ["erikarne"]


def test_checks_summary_wording() -> None:
    assert checks_summary(PullRequest(1, "t", "", "u", "b", checks_passed=2, checks_total=2)) == "2/2 passing"
    assert "1 failing" in checks_summary(
        PullRequest(1, "t", "", "u", "b", checks_passed=1, checks_total=2, checks_failing=True)
    )
    assert checks_summary(PullRequest(1, "t", "", "u", "b")) == "no checks"


def test_merge_uses_a_merge_commit_and_names_the_pr(tmp_path: Path, gh_calls) -> None:
    """The workspace merges with merge commits — 30 of the last 30."""
    read = gh_calls({})
    result = merge(tmp_path, 453)
    assert result.ok is True
    call = read()[-1]
    assert "pr merge 453" in call
    assert "--merge" in call
    assert "--admin" not in call


def test_admin_merge_is_opt_in(tmp_path: Path, gh_calls) -> None:
    read = gh_calls({})
    merge(tmp_path, 453, admin=True)
    assert "--admin" in read()[-1]


def test_a_refused_merge_reports_gits_reason(tmp_path: Path, gh_calls) -> None:
    gh_calls({}, exit_code=1)
    result = merge(tmp_path, 453)
    assert result.ok is False
    assert result.message


def test_comment_posts_the_body(tmp_path: Path, gh_calls) -> None:
    read = gh_calls({})
    result = comment(tmp_path, 453, "Add the benchmark.")
    assert result.ok is True
    assert "pr comment 453" in read()[-1]


def test_an_empty_comment_is_refused_without_calling_gh(tmp_path: Path, gh_calls) -> None:
    read = gh_calls({})
    result = comment(tmp_path, 453, "   \n  ")
    assert result.ok is False
    assert read() == []
