"""Tests for the control plane's own checkouts, driven against real repos."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from task_viewer.mirror import Mirror, MirrorError, name_from_url

from helpers import clone_repo, git, init_repo


def _task_text(title: str, state: str = "open") -> str:
    return f"---\ntitle: {title}\nstate: {state}\npriority: medium\nlabels: []\n---\n\n# {title}\n"


def _make_origin(tmp_path: Path, branch: str = "main") -> Path:
    """A bare 'GitHub' holding one task, seeded from a throwaway clone."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", branch)
    seed = init_repo(tmp_path / "seed", branch)
    task = seed / "tasks" / "open" / "001-first.md"
    task.parent.mkdir(parents=True)
    task.write_text(_task_text("First"), encoding="utf-8")
    git(seed, "add", "tasks")
    git(seed, "commit", "-m", "Add the first task")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", branch)
    return bare


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return _make_origin(tmp_path)


def _log(repo: Path, branch: str = "main") -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s", branch],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.splitlines()


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout


def _push_competing(tmp_path: Path, origin: Path, name: str) -> None:
    """Someone else pushes a commit to the origin."""
    other = tmp_path / f"other-{name}"
    if not other.exists():
        clone_repo(tmp_path, origin, other)
    else:
        git(other, "pull", "-q", "--rebase")
    (other / "tasks" / "open" / f"{name}.md").write_text(_task_text(name))
    git(other, "add", "tasks")
    git(other, "commit", "-m", f"Add {name}")
    git(other, "push", "-q", "origin", "HEAD")


def _append(root: Path, text: str = "\nAppended.\n") -> list[Path]:
    path = root / "tasks" / "open" / "001-first.md"
    path.write_text(path.read_text() + text)
    return [path]


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/arnovich/gimle-mimir.git",
        "https://github.com/arnovich/gimle-mimir",
        "git@github.com:arnovich/gimle-mimir.git",
        "/home/x/origins/gimle-mimir.git/",
    ],
)
def test_the_name_comes_from_the_url(url: str) -> None:
    assert name_from_url(url) == "gimle-mimir"


@pytest.mark.parametrize("url", ["https://x/..", "https://x/../y/..git", "-flag", "https://x/a b", ""])
def test_a_name_that_is_not_a_plain_directory_name_is_refused(url: str) -> None:
    with pytest.raises(MirrorError):
        name_from_url(url)


def test_ensure_clones_once_and_learns_the_branch(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    assert mirror.name == "origin"
    mirror.ensure()
    assert mirror.branch == "main"
    assert (mirror.tasks_dir / "open" / "001-first.md").is_file()
    mirror.ensure()  # a second call is a no-op, not a second clone


def test_ensure_works_on_a_branch_not_called_main(tmp_path: Path) -> None:
    origin = _make_origin(tmp_path, "trunk")
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    assert mirror.branch == "trunk"
    assert mirror.commit_push(_append, "task 001: note") is True
    assert _log(origin, "trunk")[0] == "task 001: note"


def test_ensure_refuses_a_directory_that_is_not_a_clone(origin: Path, tmp_path: Path) -> None:
    root = tmp_path / "data" / "origin"
    root.mkdir(parents=True)
    (root / "stray").write_text("x")
    with pytest.raises(MirrorError, match="clone failed"):
        Mirror(str(origin), root).ensure()


def test_a_symlink_in_the_repo_is_checked_out_as_a_plain_file(origin: Path, tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    os.symlink("/etc/hostname", seed / "tasks" / "open" / "002-link.md")
    git(seed, "add", "tasks")
    git(seed, "commit", "-m", "Add a link")
    git(seed, "push", "-q", "origin", "main")
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    link = mirror.tasks_dir / "open" / "002-link.md"
    assert link.is_file() and not link.is_symlink()
    assert link.read_text() == "/etc/hostname"


def test_refresh_is_lazy_until_forced_or_stale(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    first = mirror.refresh()
    assert first.fetched and first.reached and first.at is not None

    _push_competing(tmp_path, origin, "002-second")

    lazy = mirror.refresh(max_age=3600)
    assert not lazy.fetched
    assert not (mirror.tasks_dir / "open" / "002-second.md").exists()

    forced = mirror.refresh(max_age=3600, force=True)
    assert forced.fetched and forced.reached
    assert (mirror.tasks_dir / "open" / "002-second.md").is_file()

    stale = mirror.refresh(max_age=0)
    assert stale.fetched


def test_refresh_reports_an_unreachable_remote_and_keeps_the_old_tip(
    origin: Path, tmp_path: Path
) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    reached_at = mirror.last_refresh.at
    git(mirror.root, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    result = mirror.refresh(force=True)
    assert result.fetched and not result.reached
    assert result.at == reached_at
    assert "could not reach" in result.detail
    assert "could not reach" in mirror.last_refresh.detail
    assert (mirror.tasks_dir / "open" / "001-first.md").is_file()


def test_a_reset_that_fails_is_reported_and_blocks_writes(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    lock = mirror.root / ".git" / "index.lock"
    lock.write_text("")  # a crashed git left this behind
    result = mirror.refresh(force=True)
    assert result.reached and "could not be reset" in result.detail
    with pytest.raises(MirrorError, match="could not be reset"):
        mirror.commit_push(_append, "never")
    lock.unlink()
    assert mirror.refresh(force=True).detail == ""


def test_commit_push_lands_on_the_remote(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    assert mirror.commit_push(_append, "task 001: note") is True
    assert _log(origin)[0] == "task 001: note"
    assert _status(mirror.root) == ""  # left clean, at the tip it just pushed


def test_commit_push_retries_from_the_new_tip_when_someone_pushed_first(
    origin: Path, tmp_path: Path
) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    calls = {"n": 0}

    def edit(root: Path) -> list[Path]:
        calls["n"] += 1
        if calls["n"] == 1:
            # After our fetch and before our push, an agent pushes a claim.
            _push_competing(tmp_path, origin, "002-second")
        return _append(root)

    assert mirror.commit_push(edit, "task 001: append") is True
    assert calls["n"] == 2  # rejected once, reapplied on the fresh tip
    assert _log(origin)[:2] == ["task 001: append", "Add 002-second"]
    assert (mirror.tasks_dir / "open" / "002-second.md").is_file()
    assert (mirror.tasks_dir / "open" / "001-first.md").read_text().count("Appended.") == 1


def test_three_rejections_in_a_row_give_up_cleanly(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    calls = {"n": 0}

    def edit(root: Path) -> list[Path]:
        calls["n"] += 1
        _push_competing(tmp_path, origin, f"00{calls['n'] + 1}-again")
        return _append(root)

    with pytest.raises(MirrorError, match="3 times in a row"):
        mirror.commit_push(edit, "task 001: append")
    assert calls["n"] == 3
    assert _status(mirror.root) == ""
    assert "Appended." not in (mirror.tasks_dir / "open" / "001-first.md").read_text()


def test_a_push_a_hook_declines_is_not_retried(origin: Path, tmp_path: Path) -> None:
    """A protected branch says 'rejected' too, but it is not a race."""
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'no pushes today' >&2\nexit 1\n")
    hook.chmod(0o755)
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    calls = {"n": 0}

    def edit(root: Path) -> list[Path]:
        calls["n"] += 1
        return _append(root)

    with pytest.raises(MirrorError, match="no pushes today"):
        mirror.commit_push(edit, "task 001: append")
    assert calls["n"] == 1
    assert _status(mirror.root) == ""


def test_a_vanished_remote_fails_the_push_once(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    git(mirror.root, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    calls = {"n": 0}

    def edit(root: Path) -> list[Path]:
        calls["n"] += 1
        return _append(root)

    with pytest.raises(MirrorError, match="push failed"):
        mirror.commit_push(edit, "task 001: append")
    assert calls["n"] == 1
    assert _status(mirror.root) == ""


def test_commit_push_with_nothing_to_do_pushes_nothing(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    before = _log(origin)
    assert mirror.commit_push(lambda root: [], "no-op") is False
    # A path the edit names but did not change is the same as nothing.
    unchanged = lambda root: [root / "tasks" / "open" / "001-first.md"]
    assert mirror.commit_push(unchanged, "no-op") is False
    assert _log(origin) == before
    assert _status(mirror.root) == ""


def test_an_edit_that_changes_unnamed_files_is_refused(origin: Path, tmp_path: Path) -> None:
    def sneaky(root: Path) -> list[Path]:
        (root / "README.md").write_text("changed\n")
        return [root / "tasks" / "open" / "001-first.md"]

    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    with pytest.raises(MirrorError, match="outside the ones it named"):
        mirror.commit_push(sneaky, "never")
    assert _status(mirror.root) == ""


def test_a_failing_edit_leaves_the_mirror_clean(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")

    def edit(root: Path) -> list[Path]:
        (root / "tasks" / "open" / "001-first.md").write_text("garbage")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        mirror.commit_push(edit, "never")
    assert "garbage" not in (mirror.tasks_dir / "open" / "001-first.md").read_text()
    assert _status(mirror.root) == ""


def test_an_edit_outside_the_mirror_is_refused(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    stray = tmp_path / "elsewhere.md"
    stray.write_text("x")
    with pytest.raises(MirrorError, match="elsewhere.md is outside") as excinfo:
        mirror.commit_push(lambda root: [stray], "never")
    assert str(tmp_path) not in str(excinfo.value)  # no absolute paths in page text


def test_concurrent_writes_are_serialised(origin: Path, tmp_path: Path) -> None:
    """Two browser tabs, one checkout: every write lands, none interleave."""
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    results: list[bool] = []

    def write(n: int) -> None:
        results.append(mirror.commit_push(lambda root: _append(root, f"\nLine {n}.\n"), f"line {n}"))

    threads = [threading.Thread(target=write, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [True, True, True]
    assert sorted(_log(origin)[:3]) == ["line 0", "line 1", "line 2"]
    assert _status(mirror.root) == ""
    text = (mirror.tasks_dir / "open" / "001-first.md").read_text()
    assert all(f"Line {n}." in text for n in range(3))


def test_readers_holding_the_lock_see_a_whole_tree(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    with mirror.locked():
        assert (mirror.tasks_dir / "open" / "001-first.md").is_file()
        # Re-entrant for the same thread, so a reader may refresh too.
        assert mirror.refresh(max_age=3600).at is not None
