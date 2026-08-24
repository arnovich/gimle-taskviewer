"""Tests for the two operations that reach the network or move the tree.

Every remote here is a local path, so nothing in this file touches a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import commit as _commit
from helpers import git as _git
from helpers import init_repo

from task_viewer.remote import fast_forward, fetch


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A working clone of a local origin, both on ``main``."""
    origin = init_repo(tmp_path / "origin")
    _git(tmp_path, "clone", "--quiet", str(origin), str(tmp_path / "work"))
    return tmp_path / "work"


def test_fetch_reports_success(clone: Path) -> None:
    assert fetch(clone) is True


def test_fetch_of_a_repo_without_a_remote_is_a_harmless_no_op(tmp_path: Path) -> None:
    """git exits 0 here — there is simply nothing to contact, which is fine.

    The sweep on launch runs over every repo, so this has to stay quiet rather
    than being reported as a failure.
    """
    assert fetch(init_repo(tmp_path / "lonely")) is True


def test_fetch_sees_a_commit_made_on_the_origin(clone: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    assert fetch(clone) is True

    from task_viewer.git_info import load_git_info

    info = load_git_info(clone)
    assert info is not None
    assert info.unpulled == 1


def test_fast_forward_applies_what_the_remote_has(clone: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    fetch(clone)

    result = fast_forward(clone)
    assert result.ok is True
    assert "fast-forwarded 1 commit" in result.message
    assert (clone / "new.py").exists()


def test_fast_forward_says_when_there_is_nothing_to_do(clone: Path) -> None:
    result = fast_forward(clone)
    assert result.ok is True
    assert "already up to date" in result.message


def test_fast_forward_refuses_to_touch_a_dirty_tree(clone: Path, tmp_path: Path) -> None:
    """A fast-forward can still clobber an uncommitted file."""
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    fetch(clone)
    (clone / "scratch.txt").write_text("work in progress\n", encoding="utf-8")

    result = fast_forward(clone)
    assert result.ok is False
    assert "uncommitted" in result.message
    assert not (clone / "new.py").exists()  # nothing was applied


def test_fast_forward_refuses_a_diverged_branch(clone: Path, tmp_path: Path) -> None:
    """--ff-only is the safety: no merge commit, no conflicts, no surprise."""
    _commit(tmp_path / "origin", "theirs.py", "Landed upstream")
    fetch(clone)
    _commit(clone, "mine.py", "Local work on the same branch")

    result = fast_forward(clone)
    assert result.ok is False
    assert "diverged" in result.message
    assert (clone / "mine.py").exists()  # local work is untouched


def test_fast_forward_without_a_remote(tmp_path: Path) -> None:
    result = fast_forward(init_repo(tmp_path / "lonely"))
    assert result.ok is False
    assert "no remote configured" in result.message


def test_fast_forward_of_an_untracked_branch(clone: Path) -> None:
    _git(clone, "checkout", "-q", "-b", "feat/thing")
    result = fast_forward(clone)
    assert result.ok is False
    assert "no upstream" in result.message


def test_fast_forward_of_a_plain_directory(tmp_path: Path) -> None:
    result = fast_forward(tmp_path)
    assert result.ok is False
    assert "not a git checkout" in result.message
