"""Replying from tv: the entry goes to origin's default branch, never to the checkout."""

from __future__ import annotations

import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from helpers import clone_repo, git, init_repo

from task_viewer import reply
from task_viewer.app import TaskListView, TaskViewerApp, _body_of, _split_kind
from task_viewer.conversation import new_entry, open_question, parse
from task_viewer.discovery import load_tasks
from task_viewer.owner import resolve_owner
from task_viewer.remote import CommandResult
from task_viewer.reply import default_branch, find_task_file, push_entry

TASK = (
    "---\n"
    "title: Batched GPU simulation\n"
    "state: open\n"
    "priority: high\n"
    "labels: []\n"
    "---\n\n"
    "## Context\n\nThe simulator runs one stream at a time.\n\n"
    "## Outcome\n\n- under 2s on one GPU\n\n"
    "## Conversation\n\n"
    "### question · claude/abc · 2026-09-24T10:02:00Z\n\n"
    "Which GPU is the reference?\n"
)


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(seed, origin, work)``: a bare origin, the owner's clone, and a second clone that plays the agent."""
    seed = init_repo(tmp_path / "seed")
    task = seed / "tasks" / "open" / "052-batched-gpu.md"
    task.parent.mkdir(parents=True)
    task.write_text(TASK, encoding="utf-8")
    git(seed, "add", "tasks")
    git(seed, "commit", "-m", "task 052")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(seed), str(origin))
    git(seed, "remote", "add", "origin", str(origin))
    work = clone_repo(tmp_path, origin, tmp_path / "work")
    return seed, origin, work


def test_the_entry_lands_on_origin_main_and_nowhere_else(repos) -> None:
    seed, origin, work = repos
    entry = new_entry("answer", "erikarne", "The 5070 Ti.")

    result = push_entry(work, "052", entry)

    assert result.ok, result.message
    assert result.branch == "main"
    on_main = _out(origin, "show", "main:tasks/open/052-batched-gpu.md")
    assert open_question(parse(on_main)) is None
    assert "### answer · erikarne · " in on_main
    assert _out(origin, "log", "-1", "--format=%s", "main") == "task 052: answer\n"
    touched = _out(origin, "show", "--stat", "--format=", "main").strip().splitlines()
    assert touched[0].startswith("tasks/open/052-batched-gpu.md")
    assert "1 file changed" in touched[-1]
    # The owner's checkout is untouched: no edit, no stray worktree.
    local = (work / "tasks" / "open" / "052-batched-gpu.md").read_text(encoding="utf-8")
    assert "answer" not in local
    assert _out(work, "status", "--porcelain") == ""
    assert len(_out(work, "worktree", "list").splitlines()) == 1


def test_the_task_is_found_where_main_has_moved_it(repos) -> None:
    seed, origin, work = repos
    (seed / "tasks" / "ongoing").mkdir()
    git(seed, "mv", "tasks/open/052-batched-gpu.md", "tasks/ongoing/052-batched-gpu.md")
    git(seed, "commit", "-m", "task 052: claim")
    git(seed, "push", "--quiet", "origin", "HEAD:main")

    result = push_entry(work, "052", new_entry("answer", "erikarne", "The 5070 Ti."))

    assert result.ok, result.message
    assert "### answer" in _out(origin, "show", "main:tasks/ongoing/052-batched-gpu.md")
    # The stale local copy still lists it as open; that is the owner's to pull.
    assert (work / "tasks" / "open" / "052-batched-gpu.md").exists()


def test_a_directory_task_keeps_its_thread_in_description_md(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "tasks" / "open" / "010-dir-task").mkdir(parents=True)
    (tree / "tasks" / "open" / "010-dir-task" / "description.md").write_text("x", encoding="utf-8")
    (tree / "tasks" / "open" / "010-dir-task" / "plan.md").write_text("y", encoding="utf-8")
    assert find_task_file(tree, "010") == tree / "tasks" / "open" / "010-dir-task" / "description.md"
    assert find_task_file(tree, "011") is None


def test_a_task_missing_on_main_pushes_nothing(repos) -> None:
    seed, origin, work = repos
    before = _out(origin, "rev-parse", "main")

    result = push_entry(work, "999", new_entry("note", "erikarne", "hello"))

    assert not result.ok
    assert "999" in result.message
    assert _out(origin, "rev-parse", "main") == before
    assert len(_out(work, "worktree", "list").splitlines()) == 1


def test_a_rejected_push_is_retried_from_the_fresh_tip(repos, monkeypatch) -> None:
    seed, origin, work = repos
    real = reply.run_git
    raced = {"done": False}

    def racing_run_git(root, *args, **kwargs):
        if args[:1] == ("push",) and not raced["done"]:
            raced["done"] = True
            # Someone else lands on main between our fetch and our push.
            (seed / "note.txt").write_text("moved\n", encoding="utf-8")
            git(seed, "add", "note.txt")
            git(seed, "commit", "-m", "someone else moved main")
            git(seed, "push", "--quiet", "origin", "HEAD:main")
        return real(root, *args, **kwargs)

    monkeypatch.setattr(reply, "run_git", racing_run_git)
    result = push_entry(work, "052", new_entry("answer", "erikarne", "The 5070 Ti."))

    assert result.ok, result.message
    subjects = _out(origin, "log", "--format=%s", "main").splitlines()
    assert subjects[:2] == ["task 052: answer", "someone else moved main"]
    assert "### answer" in _out(origin, "show", "main:tasks/open/052-batched-gpu.md")
    assert len(_out(work, "worktree", "list").splitlines()) == 1


def test_default_branch_comes_from_origin_head_with_a_fallback(repos) -> None:
    seed, origin, work = repos
    assert default_branch(work) == "main"
    git(work, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert default_branch(work) == "main"


def test_the_owner_handle_is_one_token_without_a_slash(repos) -> None:
    """The same rule as the control plane: unsafe runs become ``-``, and a ``/`` never survives."""
    seed, origin, work = repos
    assert resolve_owner("erikarne") == "erikarne"
    assert resolve_owner("Erik Arne!") == "Erik-Arne"
    assert resolve_owner("team/erik") == "team-erik"
    git(work, "config", "user.name", "Per Repo")
    assert resolve_owner("", work) == "Per-Repo"


def test_split_kind_reads_the_first_line_when_it_names_a_kind() -> None:
    assert _split_kind("answer\n\nThe 5070 Ti.\n", "note") == ("answer", "The 5070 Ti.")
    assert _split_kind("Note\nJust saying.", "answer") == ("note", "Just saying.")
    assert _split_kind("The 5070 Ti.", "answer") == ("answer", "The 5070 Ti.")
    assert _split_kind("answer\n\n", "answer") == ("answer", "")
    # The cursor starts on the kind line, so text typed there belongs to the entry.
    assert _split_kind("answer The 5070 Ti.", "note") == ("answer", "The 5070 Ti.")
    assert _split_kind("answer: The 5070 Ti.\nMore.", "note") == ("answer", "The 5070 Ti.\nMore.")
    assert _split_kind("answers are hard", "note") == ("note", "answers are hard")


def test_body_of_drops_the_frontmatter() -> None:
    assert _body_of("---\ntitle: x\n---\n\n## Context\n") == "\n## Context\n"
    assert _body_of("## Context\n") == "## Context\n"


def _scripted_editor(tmp_path: Path, text: str) -> str:
    script = tmp_path / "editor.sh"
    script.write_text(f"#!/bin/sh\nprintf '%b' '{text}' > \"$1\"\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.mark.asyncio
async def test_pressing_a_pushes_the_answer_and_clears_the_mark(repos, tmp_path, monkeypatch) -> None:
    seed, origin, work = repos
    monkeypatch.setenv("EDITOR", _scripted_editor(tmp_path, "answer\\n\\nThe 5070 Ti.\\n"))
    monkeypatch.setenv("TV_OWNER", "erikarne")
    app = TaskViewerApp.single(work / "tasks", "work")
    async with app.run_test() as pilot:
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        assert app._tasks[0].open_question is not None

        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()

        on_main = _out(origin, "show", "main:tasks/open/052-batched-gpu.md")
        assert "### answer · erikarne · " in on_main
        # A clean checkout on main is fast-forwarded, so the list re-renders without the mark.
        assert _out(work, "rev-parse", "HEAD") == _out(origin, "rev-parse", "main")
        assert app._tasks[0].open_question is None
        assert open_question(load_tasks(work / "tasks")[0].conversation) is None


@pytest.mark.asyncio
async def test_an_empty_reply_aborts_without_touching_anything(repos, tmp_path, monkeypatch) -> None:
    seed, origin, work = repos
    monkeypatch.setenv("EDITOR", _scripted_editor(tmp_path, "answer\\n\\n"))
    before = _out(origin, "rev-parse", "main")
    drafts_before = set(Path(tempfile.gettempdir()).glob("tv-reply-052-*"))
    app = TaskViewerApp.single(work / "tasks", "work")
    async with app.run_test() as pilot:
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert _out(origin, "rev-parse", "main") == before
    # The abandoned draft is removed, not kept: there was nothing in it.
    assert set(Path(tempfile.gettempdir()).glob("tv-reply-052-*")) == drafts_before


def test_an_ambiguous_number_is_refused_unless_the_stem_picks_one(repos) -> None:
    seed, origin, work = repos
    twin = seed / "tasks" / "open" / "052-second-task.md"
    twin.write_text(TASK.replace("Batched GPU simulation", "Second"), encoding="utf-8")
    git(seed, "add", "tasks")
    git(seed, "commit", "-m", "a second 052")
    git(seed, "push", "--quiet", "origin", "HEAD:main")
    before = _out(origin, "rev-parse", "main")

    refused = push_entry(work, "052", new_entry("note", "erikarne", "hi"))
    assert not refused.ok and "ambiguous" in refused.message
    assert _out(origin, "rev-parse", "main") == before

    picked = push_entry(work, "052", new_entry("note", "erikarne", "hi"), stem="052-second-task")
    assert picked.ok, picked.message
    assert "### note" in _out(origin, "show", "main:tasks/open/052-second-task.md")
    assert "### note" not in _out(origin, "show", "main:tasks/open/052-batched-gpu.md")


def test_a_remote_rejection_is_not_retried(repos, monkeypatch) -> None:
    seed, origin, work = repos
    real = reply.run_git
    pushes = {"count": 0}

    def hook_declines(root, *args, **kwargs):
        if args[:1] == ("push",):
            pushes["count"] += 1
            return CommandResult(
                False, "", "! [remote rejected] HEAD -> main (pre-receive hook declined)"
            )
        return real(root, *args, **kwargs)

    monkeypatch.setattr(reply, "run_git", hook_declines)
    result = push_entry(work, "052", new_entry("note", "erikarne", "hi"))
    assert not result.ok and "pre-receive hook declined" in result.message
    assert pushes["count"] == 1
    assert len(_out(work, "worktree", "list").splitlines()) == 1


def test_a_fetch_that_lost_a_ref_lock_is_retried(repos, monkeypatch) -> None:
    seed, origin, work = repos
    real = reply.run_git
    fetches = {"count": 0}

    def locked_once(root, *args, **kwargs):
        if args[:1] == ("fetch",):
            fetches["count"] += 1
            if fetches["count"] == 1:
                return CommandResult(
                    False, "", "error: cannot lock ref 'refs/remotes/origin/main': is at x but expected y"
                )
        return real(root, *args, **kwargs)

    monkeypatch.setattr(reply, "run_git", locked_once)
    monkeypatch.setattr(reply.time, "sleep", lambda _seconds: None)
    result = push_entry(work, "052", new_entry("note", "erikarne", "hi"))
    assert result.ok, result.message
    assert fetches["count"] == 2


def test_a_hook_that_stages_more_keeps_the_commit_off_main(repos) -> None:
    seed, origin, work = repos
    hooks = work / _out(work, "rev-parse", "--git-path", "hooks").strip()
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\necho extra > extra.txt\ngit add extra.txt\n", encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    before = _out(origin, "rev-parse", "main")

    result = push_entry(work, "052", new_entry("note", "erikarne", "hi"))

    assert not result.ok and "a hook staged more" in result.message
    assert _out(origin, "rev-parse", "main") == before
    assert len(_out(work, "worktree", "list").splitlines()) == 1


@pytest.mark.asyncio
async def test_on_a_task_branch_the_list_shows_the_pushed_thread(repos, tmp_path, monkeypatch) -> None:
    """The checkout is not touched, yet the ? clears: the pushed thread is overlaid."""
    seed, origin, work = repos
    git(work, "checkout", "--quiet", "-b", "task/052_batched_gpu")
    monkeypatch.setenv("EDITOR", _scripted_editor(tmp_path, "answer The 5070 Ti.\n"))
    monkeypatch.setenv("TV_OWNER", "erikarne")
    app = TaskViewerApp.single(work / "tasks", "work")
    async with app.run_test() as pilot:
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert "### answer · erikarne · " in _out(origin, "show", "main:tasks/open/052-batched-gpu.md")
        assert app._tasks[0].open_question is None
        assert "The 5070 Ti." in app._tasks[0].description
        # Still on the branch, nothing written here.
        assert _out(work, "rev-parse", "--abbrev-ref", "HEAD").strip() == "task/052_batched_gpu"
        assert _out(work, "status", "--porcelain") == ""
        assert "answer" not in (work / "tasks" / "open" / "052-batched-gpu.md").read_text(encoding="utf-8")

        await pilot.press("r")  # reload from disk: the overlay survives
        await pilot.pause()
        assert app._tasks[0].open_question is None


@pytest.mark.asyncio
async def test_a_failure_inside_the_push_reports_and_frees_the_key(repos, tmp_path, monkeypatch) -> None:
    seed, origin, work = repos
    monkeypatch.setenv("EDITOR", _scripted_editor(tmp_path, "answer The 5070 Ti.\n"))
    monkeypatch.setenv("TV_OWNER", "erikarne")

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(reply, "append", boom)
    app = TaskViewerApp.single(work / "tasks", "work")
    async with app.run_test() as pilot:
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        await pilot.press("a")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._replying is False
        assert app._tasks[0].open_question is not None
        drafts = list(Path(tempfile.gettempdir()).glob("tv-reply-052-*.md"))
        assert drafts, "the draft is kept when the push fails"
        for draft in drafts:
            draft.unlink()
