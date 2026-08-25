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
    Check,
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
    assert found.reached is True
    assert list(found.by_branch) == ["task/140-derivative-augmented-token-features"]
    assert found.by_branch["task/140-derivative-augmented-token-features"].number == 453


def test_a_failure_is_not_reported_as_an_empty_queue(tmp_path: Path, gh_calls) -> None:
    """No gh, no auth, no network must not look identical to no PRs."""
    gh_calls([], exit_code=1)
    found = load_pull_requests(tmp_path)
    assert found.by_branch == {}
    assert found.reached is False
    assert found.error


def test_an_error_object_on_a_zero_exit_is_not_parsed_as_prs(
    tmp_path: Path, gh_calls
) -> None:
    """A wrapper or proxy can return {"message": ...} with exit 0."""
    gh_calls({"message": "Bad credentials"})
    found = load_pull_requests(tmp_path)
    assert found.by_branch == {}
    assert found.reached is False


def test_check_state_counts_what_failed(tmp_path: Path, gh_calls) -> None:
    gh_calls([_PR])
    pr = _only(load_pull_requests(tmp_path))
    assert pr.checks_passed == 1
    assert pr.checks_total == 2
    assert pr.checks_failing is True
    assert [c.name for c in pr.checks_failed] == ["lint"]


def test_a_pr_with_no_checks_is_not_reported_as_failing(tmp_path: Path, gh_calls) -> None:
    gh_calls([{**_PR, "statusCheckRollup": []}])
    pr = _only(load_pull_requests(tmp_path))
    assert pr.checks_total == 0
    assert pr.checks_failing is False


def test_a_pending_check_is_not_a_failure(tmp_path: Path, gh_calls) -> None:
    """A run still in flight has no conclusion; that is not a red check."""
    gh_calls([{**_PR, "statusCheckRollup": [{"name": "test", "conclusion": ""}]}])
    pr = _only(load_pull_requests(tmp_path))
    assert pr.checks_failing is False
    assert pr.checks_pending == 1


def test_bot_comments_are_separated_from_human_ones(tmp_path: Path, gh_calls) -> None:
    """Deploy-preview bots would otherwise drown the review you care about."""
    gh_calls([_PR])
    pr = _only(load_pull_requests(tmp_path))
    assert [c.author for c in pr.human_comments] == ["erikarne"]


def _pr(**kw) -> PullRequest:
    return PullRequest(1, "t", "", "u", "b", **kw)


def test_checks_summary_names_what_went_red() -> None:
    """"1 failing" does not say whether the tests broke or a preview did."""
    green = [Check("test", "SUCCESS"), Check("lint", "SUCCESS")]
    assert checks_summary(_pr(checks=green)) == "2/2 passing"
    assert checks_summary(_pr(checks=[])) == "no checks"

    mixed = [Check("test", "FAILURE"), Check("lint", "SUCCESS")]
    assert checks_summary(_pr(checks=mixed)) == "test failing"
    assert checks_summary(_pr(checks=[Check("test", "")])) == "1 running"


def test_conflicts_block_the_merge_but_red_checks_only_warn() -> None:
    """--admin bypasses branch protection, not a three-way merge."""
    conflicted = _pr(mergeable="CONFLICTING")
    assert conflicted.blocking is not None

    red = _pr(mergeable="MERGEABLE", checks=[Check("test", "FAILURE")])
    assert red.blocking is None
    assert any("test" in w for w in red.warnings)


def test_unknown_mergeability_is_a_warning_not_a_clean_bill() -> None:
    """GitHub says UNKNOWN for a while after a push."""
    assert any("not yet known" in w for w in _pr(mergeable="UNKNOWN").warnings)


def test_the_diffstat_is_carried() -> None:
    pull = _pr(changed_files=8, additions=625, deletions=41)
    assert "8 files" in pull.diffstat and "+625" in pull.diffstat


def _only(listing) -> PullRequest:
    return listing.by_branch["task/140-derivative-augmented-token-features"]


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


# --- which repository a merge is aimed at -----------------------------------


def _gh_saying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slug: str) -> None:
    """A gh whose `repo view` answers ``slug`` and whose `pr list` is empty."""
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"sys.stdout.write({slug!r} + chr(10)) if args[:2] == ['repo', 'view'] "
        "else sys.stdout.write('[]')\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])


def test_the_repo_is_named_by_gh_not_by_parsing_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirror path containing github.com/x/y resolves to a real, wrong repo.

    Aiming `gh pr merge --repo` at it would act on someone else's PR number.
    """
    from task_viewer.pull_requests import _slug

    repo = tmp_path / "work"
    repo.mkdir()
    _gh_saying(tmp_path, monkeypatch, "arnovich/the-real-one")
    assert _slug(repo) == "arnovich/the-real-one"


@pytest.mark.parametrize(
    "answer", ["", "not a slug", "-dashed/repo", "owner/repo/extra", "owner"]
)
def test_a_slug_that_is_not_owner_slash_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    from task_viewer.pull_requests import _slug

    repo = tmp_path / "work"
    repo.mkdir()
    _gh_saying(tmp_path, monkeypatch, answer)
    assert _slug(repo) == ""


def test_the_slug_is_passed_to_gh_so_a_worktree_resolves_like_its_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_viewer.pull_requests import load_pull_requests

    calls = tmp_path / "calls.log"
    binary = tmp_path / "bin" / "gh"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        f"pathlib.Path({str(calls)!r}).open('a').write(' '.join(args) + chr(10))\n"
        "sys.stdout.write('arnovich/gimle-mimir' + chr(10)) "
        "if args[:2] == ['repo', 'view'] else sys.stdout.write('[]')\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])

    repo = tmp_path / "work"
    repo.mkdir()
    load_pull_requests(repo)
    listed = [c for c in calls.read_text().splitlines() if c.startswith("pr list")]
    assert listed and listed[-1].endswith("--repo arnovich/gimle-mimir")


# --- opening a PR in a browser ----------------------------------------------


def test_the_url_is_handed_to_the_platform_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    from task_viewer import pull_requests

    launched: list[list[str]] = []
    monkeypatch.setattr(pull_requests, "_opener", lambda: "xdg-open")
    monkeypatch.setattr(
        pull_requests.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd)
    )
    result = pull_requests.open_in_browser("https://example.com/pull/1")
    assert result.ok is True
    assert launched == [["xdg-open", "https://example.com/pull/1"]]


def test_the_browser_is_detached_so_it_cannot_take_the_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console browser inheriting the TUI's terminal would hijack it."""
    from task_viewer import pull_requests

    seen: dict = {}
    monkeypatch.setattr(pull_requests, "_opener", lambda: "xdg-open")

    def fake_popen(cmd, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(pull_requests.subprocess, "Popen", fake_popen)
    pull_requests.open_in_browser("https://example.com/pull/1")
    assert seen["start_new_session"] is True
    assert seen["stdin"] == pull_requests.subprocess.DEVNULL
    assert seen["stdout"] == pull_requests.subprocess.DEVNULL


def test_a_missing_opener_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    from task_viewer import pull_requests

    monkeypatch.setattr(pull_requests, "_opener", lambda: "")
    result = pull_requests.open_in_browser("https://example.com/pull/1")
    assert result.ok is False
    assert "no way to open" in result.message


def test_an_opener_that_will_not_start_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from task_viewer import pull_requests

    monkeypatch.setattr(pull_requests, "_opener", lambda: "xdg-open")

    def boom(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(pull_requests.subprocess, "Popen", boom)
    result = pull_requests.open_in_browser("https://example.com/pull/1")
    assert result.ok is False


@pytest.mark.parametrize("url", ["", "not-a-url", "javascript:alert(1)", "file:///etc/passwd"])
def test_only_http_urls_are_opened(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """The URL comes from an API response; do not hand anything to a shell-less
    opener that is not plainly a web page."""
    from task_viewer import pull_requests

    launched: list = []
    monkeypatch.setattr(pull_requests, "_opener", lambda: "xdg-open")
    monkeypatch.setattr(
        pull_requests.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd)
    )
    result = pull_requests.open_in_browser(url)
    assert result.ok is False
    assert launched == []
