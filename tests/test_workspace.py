"""Tests for workspace (multi-project) discovery and browsing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from task_viewer.app import (
    TaskListView,
    TaskViewerApp,
    _format_project_row,
    _git_section,
)
from task_viewer.git_info import load_git_info
from textual.widgets import Label
from task_viewer.workspace import find_projects


def _make_project(root: Path, name: str, open_ids: list[str]) -> None:
    open_dir = root / name / "tasks" / "open"
    open_dir.mkdir(parents=True)
    for task_id in open_ids:
        (open_dir / f"{task_id}.md").write_text(f"# {task_id}\n")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "gimle"
    _make_project(root, "gimle-asgard", ["001-a", "002-b"])
    _make_project(root, "gimle-mimir", ["010-c"])
    (root / "not-a-project").mkdir()  # no tasks/ -> ignored
    (root / ".hidden").mkdir()
    return root


def test_find_projects_lists_only_task_folders(workspace: Path) -> None:
    projects = find_projects(workspace)
    assert [p.name for p in projects] == ["gimle-asgard", "gimle-mimir"]


@pytest.mark.asyncio
async def test_browse_into_and_back_out_of_a_project(workspace: Path) -> None:
    projects = find_projects(workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        assert app._level == "projects"
        list_view = app.query_one(TaskListView)
        assert len(list_view) == 2  # two projects

        # Step into the first project -> its two open tasks.
        await pilot.press("right")
        await pilot.pause()
        assert app._level == "tasks"
        assert len(list_view) == 2
        assert app._current_project.name == "gimle-asgard"

        # Step back out -> project list, reselected on the one we came from.
        await pilot.press("left")
        await pilot.pause()
        assert app._level == "projects"
        assert len(list_view) == 2


@pytest.mark.asyncio
async def test_task_keys_hidden_while_browsing_projects(workspace: Path) -> None:
    projects = find_projects(workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        # At project level, task-level actions are disabled...
        assert app.check_action("groom", ()) is None
        assert app.check_action("enter_project", ()) is True
        # ...and enabled once inside a project.
        await pilot.press("right")
        await pilot.pause()
        assert app.check_action("groom", ()) is True
        assert app.check_action("back", ()) is True


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def worktree_workspace(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace where one project is a worktree of another, one commit ahead."""
    for name, value in (
        ("GIT_CONFIG_GLOBAL", "/dev/null"),
        ("GIT_CONFIG_SYSTEM", "/dev/null"),
        ("GIT_AUTHOR_NAME", "tv test"),
        ("GIT_AUTHOR_EMAIL", "tv@example.com"),
        ("GIT_COMMITTER_NAME", "tv test"),
        ("GIT_COMMITTER_EMAIL", "tv@example.com"),
    ):
        monkeypatch.setenv(name, value)

    repo = workspace / "gimle-asgard"
    _git(repo, "init", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Initial commit")

    worktree = workspace / "gimle-asgard-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(worktree))
    (worktree / "tasks" / "open" / "003-new.md").write_text("# New task\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "Add the feature")
    return workspace


def test_project_row_shows_branch_drift_and_age(worktree_workspace: Path) -> None:
    project = next(
        p for p in find_projects(worktree_workspace) if p.name.endswith("-feature")
    )
    row = _format_project_row(project, load_git_info(project.path))
    branch_line = row.splitlines()[1]
    assert "feat/thing" in branch_line
    assert "↑1" in branch_line


def test_summary_section_answers_created_updated_and_drift(
    worktree_workspace: Path,
) -> None:
    worktree = worktree_workspace / "gimle-asgard-feature"
    section = "\n".join(_git_section(load_git_info(worktree)))
    assert "## Worktree" in section
    assert "worktree of `gimle-asgard`" in section
    assert "**Created**" in section
    assert "**Updated**" in section
    assert "1 ahead" in section
    assert "- Add the feature" in section


def test_no_git_section_for_a_plain_project(workspace: Path) -> None:
    project = find_projects(workspace)[0]
    assert _git_section(load_git_info(project.path)) == []
    assert "\n" not in _format_project_row(project, None)


@pytest.mark.asyncio
async def test_workspace_subtitle_counts_worktrees(worktree_workspace: Path) -> None:
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        # The git scan runs in a worker so the list paints immediately...
        assert "worktree" not in app.sub_title
        await app.workers.wait_for_complete()
        await pilot.pause()
        # ...and the rows are repainted once it lands.
        assert "1 worktree" in app.sub_title
        assert app._git_info[worktree_workspace / "gimle-asgard-feature"] is not None


@pytest.mark.asyncio
async def test_project_rows_are_repainted_after_the_scan(
    worktree_workspace: Path,
) -> None:
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        labels = [str(label.render()) for label in app.query(Label)]
        assert any("feat/thing" in text for text in labels)
