"""Tests for reading a repository's task history out of git."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_viewer.history import Commit, branch_number, branch_tips, task_commits

from helpers import git, init_repo


def _commit(subject: str, paths=(), body: str = "") -> Commit:
    return Commit("abc", "erikarne", datetime(2026, 9, 24, tzinfo=timezone.utc), subject, tuple(paths), body)


def test_the_subject_says_what_happened_to_which_task() -> None:
    assert (_commit("task 053: claim").number, _commit("task 053: claim").verb) == ("053", "claimed")
    assert _commit("task 53: plan synced").verb == "planned"
    assert _commit("task 053: closed").verb == "closed"
    assert _commit("task 053: answer from erikarne").verb == "answer"
    assert _commit("task 053: note from erikarne").verb == "note"
    assert _commit("task 053: queued first").verb == "queued first"
    assert _commit("task 053: queued").verb == "queued"
    assert _commit("task 053: unqueued").verb == "unqueued"
    assert _commit("task 053: released — needs the owner").verb == "released"
    assert _commit("task 053: filed").verb == "filed"
    assert _commit("task 053: take main's task file").verb == "changed"
    assert _commit("task 053: claim").detail == "claim"
    assert not _commit("task 053: claim").says_more_than_its_verb
    assert not _commit("task 053: closed").says_more_than_its_verb
    assert _commit("task 053: closed — PR #61 opened").says_more_than_its_verb


def test_a_github_merge_names_its_branch_and_pull_request() -> None:
    merge = _commit("Merge pull request #453 from arnovich/task/140_derivative_features")
    assert (merge.verb, merge.number, merge.pull_request) == ("merged", "140", 453)
    assert _commit("Merge branch 'feat/x'").verb == "changed"
    squash = _commit("task 140: derivative features (#453)")
    assert (squash.verb, squash.number, squash.pull_request) == ("merged", "140", 453)
    assert squash.title == "derivative features"
    # GitHub keeps the PR title in the merge commit's body.
    github = _commit(
        "Merge pull request #625 from arnovich/feat/role_aware_inputs",
        body="Migrate chained simulation to role-aware inputs\n\nLonger description.",
    )
    assert github.title == "Migrate chained simulation to role-aware inputs"
    assert github.branch == "feat/role_aware_inputs" and github.number is None
    assert merge.title == "task/140_derivative_features"  # no body: the branch is the message


def test_an_unlabelled_commit_is_placed_by_the_files_it_touched() -> None:
    commit = _commit("Reword the outcome", ["tasks/open/061-model-architecture.md"])
    assert (commit.number, commit.verb) == ("061", "changed")
    assert _commit("Unrelated", ["README.md"]).number is None
    assert not _commit("Unrelated", ["README.md"]).about_tasks


def test_branch_numbers_survive_either_slug_spelling() -> None:
    assert branch_number("task/053_batched_gpu") == "053"
    assert branch_number("task/180-behavioral-loss-clean-ab") == "180"
    assert branch_number("feat/no_number") is None


def test_the_log_is_read_newest_first_with_merges_included(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    task = repo / "tasks" / "open" / "001-first.md"
    task.parent.mkdir(parents=True)
    task.write_text("# First\n")
    git(repo, "add", "tasks"); git(repo, "commit", "-m", "task 001: filed")
    git(repo, "checkout", "-q", "-b", "task/001_first")
    (repo / "code.py").write_text("x\n"); git(repo, "add", "code.py"); git(repo, "commit", "-m", "Implement")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-m", "Merge pull request #1 from me/task/001_first\n\nThe first thing", "task/001_first")
    task.write_text("# First\n\nmore\n"); git(repo, "add", "tasks"); git(repo, "commit", "-m", "task 001: closed")

    commits = task_commits(repo)
    assert [c.verb for c in commits] == ["closed", "merged", "filed"]
    assert commits[1].pull_request == 1 and commits[1].number == "001"
    assert commits[1].title == "The first thing" and commits[1].body.startswith("The first thing")
    assert commits[0].paths == ("tasks/open/001-first.md",)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert task_commits(repo, since=tomorrow) == []


def test_remote_task_branches_and_their_tips(tmp_path: Path) -> None:
    origin = init_repo(tmp_path / "origin")
    git(origin, "branch", "task/007_thing")
    git(origin, "branch", "feat/other")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    tips = branch_tips(clone)
    assert list(tips) == ["task/007_thing"]
    assert tips["task/007_thing"].tzinfo is not None
    assert branch_tips(tmp_path / "not-a-repo") == {}
