"""Tests for the git summary shown against each project in a workspace.

These build real repositories in ``tmp_path`` — the module is a thin wrapper
over the ``git`` CLI, so faking the CLI would only test the fake.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from task_viewer.git_info import describe_age, load_git_info


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _commit(cwd: Path, name: str, message: str) -> None:
    (cwd / name).write_text(f"{name}\n", encoding="utf-8")
    _git(cwd, "add", name)
    _git(cwd, "commit", "-m", message)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's real git config out of the test repositories."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "tv test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "tv@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "tv test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "tv@example.com")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on ``main`` with one commit."""
    root = tmp_path / "gimle-example"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _commit(root, "README.md", "Initial commit")
    return root


def test_returns_none_for_a_plain_directory(tmp_path: Path) -> None:
    assert load_git_info(tmp_path) is None


def test_reads_the_main_checkout(repo: Path) -> None:
    info = load_git_info(repo)
    assert info is not None
    assert info.branch == "main"
    assert info.is_worktree is False
    assert info.repo is None
    # Nothing to compare a lone `main` against, so no base and no drift.
    assert info.base is None
    assert (info.ahead, info.behind) == (0, 0)
    assert info.updated is not None


def test_worktree_reports_its_age_branch_and_parent_repo(repo: Path) -> None:
    worktree = repo.parent / "gimle-example-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(worktree))

    info = load_git_info(worktree)
    assert info is not None
    assert info.is_worktree is True
    assert info.branch == "feat/thing"
    assert info.repo == "gimle-example"
    assert info.created is not None
    assert info.kind == "worktree"


def test_worktree_drift_from_main_counts_both_directions(repo: Path) -> None:
    worktree = repo.parent / "gimle-example-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(worktree))
    _commit(worktree, "feature.py", "Add the feature")
    _commit(worktree, "more.py", "Polish the feature")
    _commit(repo, "hotfix.py", "Fix something on main")

    info = load_git_info(worktree)
    assert info is not None
    assert info.base == "main"
    assert info.ahead == 2
    assert info.behind == 1
    # Newest first, so the branch reads like a changelog.
    assert info.subjects == ["Polish the feature", "Add the feature"]


def test_uncommitted_work_is_counted_and_dates_the_checkout(repo: Path) -> None:
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    # A space and a non-ASCII character: git C-quotes these unless asked not to,
    # and a quoted name no longer names a file we can stat.
    scratch = repo / "næste plan.md"
    scratch.write_text("new\n", encoding="utf-8")
    touched = datetime.now(timezone.utc).astimezone() + timedelta(days=1)
    os.utime(scratch, (touched.timestamp(), touched.timestamp()))

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 2
    # The newest edit dates the checkout, ahead of the last commit.
    assert info.updated is not None
    assert info.updated > _last_commit_time(repo)
    assert abs((info.updated - touched).total_seconds()) < 1


def test_a_staged_rename_counts_once(repo: Path) -> None:
    _git(repo, "mv", "README.md", "READETC.md")

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 1


def test_base_falls_back_to_master(tmp_path: Path) -> None:
    root = tmp_path / "old-style"
    root.mkdir()
    _git(root, "init", "-b", "master")
    _commit(root, "README.md", "Initial commit")
    _git(root, "checkout", "-b", "feat/thing")
    _commit(root, "feature.py", "Add the feature")

    info = load_git_info(root)
    assert info is not None
    assert info.base == "master"
    assert info.ahead == 1


def test_detached_head_has_no_branch(repo: Path) -> None:
    _git(repo, "checkout", "--detach", "HEAD")
    assert load_git_info(repo) is None


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=5), "5m"),
        (timedelta(hours=3), "3h"),
        (timedelta(days=2), "2d"),
        (timedelta(days=21), "3w"),
        (timedelta(days=200), "6mo"),
    ],
)
def test_describe_age_units(delta: timedelta, expected: str) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    assert describe_age(now - delta, now=now) == expected


def test_describe_age_of_nothing() -> None:
    assert describe_age(None) == "—"


def _last_commit_time(root: Path) -> datetime:
    stamp = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%cI"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return datetime.fromisoformat(stamp)
