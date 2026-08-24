"""Tests for the two operations that reach the network or move the tree.

Every remote here is a local path, so nothing in this file touches a network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import commit as _commit
from helpers import git as _git
from helpers import init_repo

from task_viewer.git_info import load_git_info
from task_viewer.remote import fast_forward, fetch


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A working clone of a local origin, both on ``main``."""
    origin = init_repo(tmp_path / "origin")
    _git(tmp_path, "clone", "--quiet", str(origin), str(tmp_path / "work"))
    return tmp_path / "work"


def test_fetch_reports_success(clone: Path) -> None:
    assert fetch(clone) is True


def test_fetch_of_an_unreachable_remote_reports_failure(tmp_path: Path) -> None:
    """The caller has to be able to tell; a failed fetch still bumps FETCH_HEAD."""
    repo = init_repo(tmp_path / "orphan")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist"))
    assert fetch(repo) is False


def test_fetch_gives_up_at_its_timeout(clone: Path, monkeypatch) -> None:
    import subprocess as sp

    class _Hang:
        """A child that never answers — patched in for both Popen and run()."""

        returncode = 0

        def communicate(self, *a, **kw):
            raise sp.TimeoutExpired("git", 1)

        def kill(self):
            pass

        def wait(self, *a, **kw):
            return 0

        def poll(self):
            return 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("task_viewer.remote.subprocess.Popen", lambda *a, **kw: _Hang())
    assert fetch(clone, timeout=0.1) is False


def test_git_is_never_allowed_to_ask_a_human(clone: Path, monkeypatch) -> None:
    """A prompt inside a full-screen TUI hangs it; an askpass dialog covers it."""
    seen = {}

    real_popen = __import__("subprocess").Popen

    def _spy(command, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real_popen(command, **kwargs)

    monkeypatch.setattr("task_viewer.remote.subprocess.Popen", _spy)
    fetch(clone)
    assert seen["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["GIT_ASKPASS"] == ""
    assert seen["SSH_ASKPASS_REQUIRE"] == "never"
    assert "BatchMode=yes" in seen["GIT_SSH_COMMAND"]


def test_the_users_own_ssh_command_is_preserved(clone: Path, monkeypatch) -> None:
    """It may carry the deploy key this remote actually needs."""
    monkeypatch.setattr(
        "task_viewer.remote._config", lambda key: "ssh -i /keys/deploy"
    )
    seen = {}
    real_popen = __import__("subprocess").Popen

    def _spy(command, **kwargs):
        seen.update(kwargs.get("env") or {})
        return real_popen(command, **kwargs)

    monkeypatch.setattr("task_viewer.remote.subprocess.Popen", _spy)
    fetch(clone)
    assert seen["GIT_SSH_COMMAND"] == "ssh -i /keys/deploy -oBatchMode=yes"


def test_fetch_prunes_a_branch_deleted_on_the_remote(clone: Path, tmp_path: Path) -> None:
    """Without --prune a deleted branch keeps a stale ref and never reads gone."""
    origin = tmp_path / "origin"
    _git(origin, "branch", "feat/tidy")
    fetch(clone)
    _git(clone, "checkout", "-q", "-b", "feat/tidy", "--track", "origin/feat/tidy")
    _git(origin, "branch", "-D", "feat/tidy")

    fetch(clone)
    info = load_git_info(clone)
    assert info is not None
    assert info.upstream_gone is True


def test_fetch_of_a_repo_without_a_remote_is_a_harmless_no_op(tmp_path: Path) -> None:
    """git exits 0 here — there is simply nothing to contact, which is fine.

    The sweep on launch runs over every repo, so this has to stay quiet rather
    than being reported as a failure.
    """
    assert fetch(init_repo(tmp_path / "lonely")) is True


def test_fetch_sees_a_commit_made_on_the_origin(clone: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    assert fetch(clone) is True

    info = load_git_info(clone)
    assert info is not None
    assert info.unpulled == 1


def test_fast_forward_applies_what_the_remote_has(clone: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    fetch(clone)

    result = fast_forward(clone)
    assert result.ok is True
    # Exact: "fast-forwarded 1 commit" is a substring of "…1 commits".
    assert result.message == "fast-forwarded 1 commit"
    assert (clone / "new.py").exists()


def test_fast_forward_pluralises_more_than_one_commit(clone: Path, tmp_path: Path) -> None:
    _commit(tmp_path / "origin", "a.py", "One")
    _commit(tmp_path / "origin", "b.py", "Two")
    fetch(clone)

    assert fast_forward(clone, refresh=False).message == "fast-forwarded 2 commits"


def test_fast_forward_fetches_before_deciding(clone: Path, tmp_path: Path) -> None:
    """Otherwise `already up to date` is measured against stale refs."""
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    # Note: no fetch here — fast_forward must do it itself.
    result = fast_forward(clone)
    assert result.ok is True
    assert result.message == "fast-forwarded 1 commit"


def test_fast_forward_says_when_there_is_nothing_to_do(clone: Path) -> None:
    result = fast_forward(clone)
    assert result.ok is True
    assert "up to date" in result.message
    assert "as of" in result.message  # never claims more than it checked


def test_fast_forward_refuses_to_touch_a_dirty_tree(clone: Path, tmp_path: Path) -> None:
    """A fast-forward can still clobber an uncommitted file."""
    _commit(tmp_path / "origin", "new.py", "Landed upstream")
    fetch(clone)
    (clone / "scratch.txt").write_text("work in progress\n", encoding="utf-8")

    result = fast_forward(clone, refresh=False)
    assert result.ok is False
    assert result.message == "1 uncommitted file — commit or stash first"
    assert not (clone / "new.py").exists()  # nothing was applied


def test_fast_forward_refuses_a_diverged_branch(clone: Path, tmp_path: Path) -> None:
    """--ff-only is the safety: no merge commit, no conflicts, no surprise."""
    _commit(tmp_path / "origin", "theirs.py", "Landed upstream")
    fetch(clone)
    _commit(clone, "mine.py", "Local work on the same branch")

    result = fast_forward(clone)
    assert result.ok is False
    # git's own words, not a guess about which of many refusals this was.
    assert "iverging" in result.message or "ff-only" in result.message
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


def test_fast_forward_refuses_a_branch_tracking_someone_elses(
    clone: Path, tmp_path: Path
) -> None:
    """`git worktree add -b` leaves task branches tracking origin/main.

    Fast-forwarding then moves the branch onto main and loses what it was for.
    """
    _commit(tmp_path / "origin", "theirs.py", "Landed on main")
    _git(clone, "checkout", "-q", "-b", "task/143")
    _git(clone, "branch", "--set-upstream-to=origin/main", "task/143")
    fetch(clone)

    result = fast_forward(clone, refresh=False)
    assert result.ok is False
    assert "not a branch of its own" in result.message
    assert not (clone / "theirs.py").exists()  # nothing was applied


def test_fast_forward_refuses_a_deleted_upstream(clone: Path) -> None:
    _git(clone, "checkout", "-q", "-b", "feat/tidy")
    _git(clone, "update-ref", "refs/remotes/origin/feat/tidy", "HEAD")
    _git(clone, "branch", "--set-upstream-to=origin/feat/tidy", "feat/tidy")
    _git(clone, "update-ref", "-d", "refs/remotes/origin/feat/tidy")

    result = fast_forward(clone, refresh=False)
    assert result.ok is False
    assert "no longer exists" in result.message


def test_fast_forward_does_not_destroy_an_ignored_file(
    clone: Path, tmp_path: Path
) -> None:
    """git silently overwrites ignored files; a local .env lives nowhere else."""
    (clone / ".gitignore").write_text(".env\n", encoding="utf-8")
    _git(clone, "add", ".gitignore")
    _git(clone, "commit", "-m", "Ignore .env")
    (clone / ".env").write_text("SECRET=local-only\n", encoding="utf-8")

    origin = tmp_path / "origin"
    (origin / ".env").write_text("FROM_REMOTE\n", encoding="utf-8")
    _git(origin, "add", "-f", ".env")
    _git(origin, "commit", "-m", "Add a tracked .env upstream")
    fetch(clone)

    result = fast_forward(clone, refresh=False)
    assert result.ok is False
    assert ".env" in result.message
    assert (clone / ".env").read_text() == "SECRET=local-only\n"


def test_fast_forward_reports_gits_own_reason(clone: Path, tmp_path: Path) -> None:
    """A refusal caused by local edits must not be reported as 'diverged'."""
    _commit(tmp_path / "origin", "shared.py", "Change a file upstream")
    fetch(clone)
    (clone / "shared.py").write_text("local edit\n", encoding="utf-8")
    _git(clone, "add", "shared.py")
    _git(clone, "commit", "-m", "stage it so the tree is clean")
    _git(clone, "reset", "--soft", "HEAD~1")  # staged, so status is dirty again

    result = fast_forward(clone, refresh=False)
    assert result.ok is False
    assert "uncommitted" in result.message
