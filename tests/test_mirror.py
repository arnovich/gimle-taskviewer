"""Tests for the control plane's own checkouts, driven against real repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.mirror import Mirror, MirrorError, name_from_url

from helpers import clone_repo, git, init_repo


def _task_text(title: str, state: str = "open") -> str:
    return f"---\ntitle: {title}\nstate: {state}\npriority: medium\nlabels: []\n---\n\n# {title}\n"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare 'GitHub' holding one task, plus a seed clone to push more from."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    seed = init_repo(tmp_path / "seed")
    task = seed / "tasks" / "open" / "001-first.md"
    task.parent.mkdir(parents=True)
    task.write_text(_task_text("First"), encoding="utf-8")
    git(seed, "add", "tasks")
    git(seed, "commit", "-m", "Add the first task")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return bare


def _log(repo: Path) -> list[str]:
    import subprocess

    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s", "main"],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.splitlines()


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


def test_ensure_clones_once_and_learns_the_branch(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    assert mirror.name == "origin"
    mirror.ensure()
    assert mirror.branch == "main"
    assert (mirror.tasks_dir / "open" / "001-first.md").is_file()
    mirror.ensure()  # a second call is a no-op, not a second clone


def test_refresh_is_lazy_until_forced_or_stale(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    first = mirror.refresh()
    assert first.fetched and first.reached and first.at is not None

    # Someone pushes an answer from their editor.
    other = clone_repo(tmp_path, origin, tmp_path / "other")
    (other / "tasks" / "open" / "002-second.md").write_text(_task_text("Second"))
    git(other, "add", "tasks")
    git(other, "commit", "-m", "Add a second task")
    git(other, "push", "-q", "origin", "main")

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
    assert (mirror.tasks_dir / "open" / "001-first.md").is_file()


def test_commit_push_lands_on_the_remote(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")

    def edit(root: Path) -> list[Path]:
        path = root / "tasks" / "open" / "001-first.md"
        path.write_text(path.read_text() + "\n## Conversation\n\n### note · me · 2026-01-01\n\nHi.\n")
        return [path]

    assert mirror.commit_push(edit, "task 001: note") is True
    assert _log(origin)[0] == "task 001: note"
    # The mirror is left clean, at the tip it just pushed.
    import subprocess
    status = subprocess.run(
        ["git", "-C", str(mirror.root), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert status == ""


def test_commit_push_retries_from_the_new_tip_when_someone_pushed_first(
    origin: Path, tmp_path: Path
) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    mirror.refresh()
    other = clone_repo(tmp_path, origin, tmp_path / "other")
    calls = {"n": 0}

    def edit(root: Path) -> list[Path]:
        calls["n"] += 1
        if calls["n"] == 1:
            # Between our fetch and our push, an agent claims a task.
            (other / "tasks" / "open" / "002-second.md").write_text(_task_text("Second"))
            git(other, "add", "tasks")
            git(other, "commit", "-m", "Add a second task")
            git(other, "push", "-q", "origin", "main")
        path = root / "tasks" / "open" / "001-first.md"
        path.write_text(path.read_text() + "\nAppended.\n")
        return [path]

    # The edit is applied to the stale tree first, then reapplied to the fresh one.
    def racing_edit(root: Path) -> list[Path]:
        return edit(root)

    # Make the first push actually race: fetch happens in commit_push, so we
    # arrange the competing push inside the edit callback (after the fetch).
    assert mirror.commit_push(racing_edit, "task 001: append") is True
    assert calls["n"] == 2
    assert _log(origin)[:2] == ["task 001: append", "Add a second task"]
    assert (mirror.tasks_dir / "open" / "002-second.md").is_file()
    assert "Appended." in (mirror.tasks_dir / "open" / "001-first.md").read_text()


def test_commit_push_with_nothing_to_do_pushes_nothing(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    before = _log(origin)
    assert mirror.commit_push(lambda root: [], "no-op") is False
    assert _log(origin) == before


def test_a_failing_edit_leaves_the_mirror_clean(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")

    def edit(root: Path) -> list[Path]:
        (root / "tasks" / "open" / "001-first.md").write_text("garbage")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        mirror.commit_push(edit, "never")
    assert "garbage" not in (mirror.tasks_dir / "open" / "001-first.md").read_text()


def test_an_edit_outside_the_mirror_is_refused(origin: Path, tmp_path: Path) -> None:
    mirror = Mirror(str(origin), tmp_path / "data" / "origin")
    stray = tmp_path / "elsewhere.md"
    stray.write_text("x")
    with pytest.raises(MirrorError):
        mirror.commit_push(lambda root: [stray], "never")
