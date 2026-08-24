"""Tests for the git summary shown against each project in a workspace.

These build real repositories in ``tmp_path`` — the module is a thin wrapper
over the ``git`` CLI, so faking the CLI would only test the fake.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import commit as _commit
from helpers import git as _git
from helpers import init_repo

from task_viewer.git_info import (
    _MAX_SUBJECTS,
    describe_age,
    format_moment,
    load_git_info,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on ``main`` with one commit."""
    return init_repo(tmp_path / "gimle-example")


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
    # A whole second, so a filesystem with coarse mtime granularity still
    # stores it exactly and the comparison below can be exact.
    stamp = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
    os.utime(scratch, (stamp, stamp))
    touched = datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone()

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 2
    # The newest edit dates the checkout, ahead of the last commit.
    assert info.updated is not None
    assert info.updated > _last_commit_time(repo)
    assert info.updated == touched


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


def test_detached_head_is_still_described(repo: Path) -> None:
    """A mid-rebase or mid-bisect checkout is exactly what you want to see."""
    _git(repo, "checkout", "--detach", "HEAD")

    info = load_git_info(repo)
    assert info is not None
    assert info.branch.startswith("detached at ")


def test_empty_repository_has_nothing_to_describe(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-b", "main")
    assert load_git_info(root) is None


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=30), "now"),
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
    from task_viewer.git_info import _last_commit

    stamp = _last_commit(root)
    assert stamp is not None
    return stamp


def test_merged_worktree_is_recognised(repo: Path) -> None:
    """`ahead == 0` means main already holds everything — the worktree can go."""
    worktree = repo.parent / "gimle-example-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(worktree))
    _commit(worktree, "feature.py", "Add the feature")
    _git(repo, "merge", "--no-ff", "-m", "Merge the feature", "feat/thing")

    info = load_git_info(worktree)
    assert info is not None
    assert info.merged is True
    assert info.ahead == 0
    assert info.behind == 1  # only main's merge commit
    assert info.subjects == []


def test_unmerged_worktree_is_not_marked_merged(repo: Path) -> None:
    worktree = repo.parent / "gimle-example-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(worktree))
    _commit(worktree, "feature.py", "Add the feature")

    info = load_git_info(worktree)
    assert info is not None
    assert info.merged is False


def test_created_is_unknown_when_the_reflog_was_trimmed(repo: Path) -> None:
    """The oldest surviving entry is usually an old pull, not a birth date."""
    log = repo / ".git" / "logs" / "refs" / "heads" / "main"
    entry = log.read_text(encoding="utf-8").split("\t")[0]
    log.write_text(f"{entry}\tpull origin main: Fast-forward\n", encoding="utf-8")

    info = load_git_info(repo)
    assert info is not None
    assert info.created is None


def test_created_is_kept_for_a_real_clone_entry(repo: Path) -> None:
    log = repo / ".git" / "logs" / "refs" / "heads" / "main"
    entry = log.read_text(encoding="utf-8").split("\t")[0]
    log.write_text(f"{entry}\tclone: from https://example.com/x.git\n", encoding="utf-8")

    info = load_git_info(repo)
    assert info is not None
    assert info.created is not None


def test_a_tag_named_main_does_not_shadow_the_branch(repo: Path) -> None:
    """git resolves refs/tags before refs/heads, which would skew every count."""
    _git(repo, "checkout", "-b", "feat/thing")
    _commit(repo, "feature.py", "Add the feature")
    _git(repo, "tag", "-f", "main", "HEAD")

    info = load_git_info(repo)
    assert info is not None
    assert info.base == "main"
    assert info.ahead == 1  # against the branch, not the tag sitting on HEAD


def test_a_legacy_master_does_not_outrank_origin_main(repo: Path) -> None:
    """An orphaned `master` left over from a rename must not become the base."""
    _git(repo, "branch", "master")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-b", "feat/thing")
    _commit(repo, "feature.py", "Add the feature")

    info = load_git_info(repo)
    assert info is not None
    assert info.base == "main"


def test_a_submodule_is_not_mistaken_for_a_worktree(repo: Path, tmp_path: Path) -> None:
    """A submodule also has a `.git` *file*, but is not a linked worktree."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _git(inner, "init", "-b", "main")
    _commit(inner, "lib.py", "Initial commit")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub")

    info = load_git_info(repo / "sub")
    assert info is not None
    assert info.is_worktree is False
    assert info.repo is None


def test_a_non_utf8_filename_does_not_crash(repo: Path) -> None:
    """Paths are bytes; `-z` hands them over without git's C-quoting."""
    (repo / os.fsdecode(b"rapport-caf\xe9.txt")).write_bytes(b"x")

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 1


def test_an_untracked_directory_counts_its_files(repo: Path) -> None:
    """The default status collapses a whole tree into one stale `dir/` record."""
    scratch = repo / "scratch"
    scratch.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (scratch / name).write_text("x\n", encoding="utf-8")

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 3


def test_an_out_of_range_timestamp_is_not_a_date(repo: Path) -> None:
    log = repo / ".git" / "logs" / "refs" / "heads" / "main"
    sha = log.read_text(encoding="utf-8").split(" ")[1]
    log.write_text(
        f"{'0' * 40} {sha} tv test <tv@example.com> 99999999999999999999 +0000"
        "\tclone: from https://example.com/x.git\n",
        encoding="utf-8",
    )

    info = load_git_info(repo)
    assert info is not None
    assert info.created is None


def test_subjects_are_capped_but_the_count_is_not(repo: Path) -> None:
    _git(repo, "checkout", "-b", "feat/thing")
    for n in range(_MAX_SUBJECTS + 2):
        _commit(repo, f"f{n}.py", f"Commit {n}")

    info = load_git_info(repo)
    assert info is not None
    assert info.ahead == _MAX_SUBJECTS + 2
    assert len(info.subjects) == _MAX_SUBJECTS
    assert info.subjects[0] == f"Commit {_MAX_SUBJECTS + 1}"  # newest first


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(days=-1), "in the future"),
    ],
)
def test_format_moment_reads_as_a_sentence(delta: timedelta, expected: str) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    assert format_moment(now - delta, now=now).endswith(f"({expected})")


def test_format_moment_of_nothing() -> None:
    assert format_moment(None) == "unknown"


def test_base_falls_back_to_origin_main_while_on_main(repo: Path) -> None:
    """The whole reason the candidate list has remote entries: unpushed work."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _commit(repo, "one.py", "First unpushed commit")
    _commit(repo, "two.py", "Second unpushed commit")

    info = load_git_info(repo)
    assert info is not None
    assert info.base == "origin/main"
    assert info.ahead == 2
    assert info.merged is False


def test_a_vanished_dirty_path_is_ignored(repo: Path) -> None:
    """A dangling symlink is reported by status but cannot be stat'ed."""
    (repo / "dangling").symlink_to(repo / "gone.txt")

    info = load_git_info(repo)
    assert info is not None
    assert info.dirty == 1
    assert info.updated is not None  # the commit still dates it


def test_returns_none_when_git_is_unavailable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert load_git_info(repo) is None


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(minutes=59), "59m"),
        (timedelta(minutes=60), "1h"),
        (timedelta(hours=23), "23h"),
        (timedelta(hours=24), "1d"),
        (timedelta(days=13), "13d"),
        (timedelta(days=14), "2w"),
        (timedelta(days=89), "12w"),
        (timedelta(days=90), "3mo"),
    ],
)
def test_describe_age_bucket_boundaries(delta: timedelta, expected: str) -> None:
    """Choosing the bucket is the whole job, so pin both sides of each edge."""
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    assert describe_age(now - delta, now=now) == expected
