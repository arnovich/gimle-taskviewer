"""Tests for workspace (multi-project) discovery and browsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.app import TaskListView, TaskViewerApp, _format_project_row
from helpers import git as _git
from textual.widgets import Label, Markdown
from task_viewer.git_info import load_git_info
from task_viewer.workspace import find_projects, group_projects


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


@pytest.fixture
def worktree_workspace(workspace: Path) -> Path:
    """A workspace where one project is a worktree of another, one commit ahead."""
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


def test_project_row_shows_branch_and_drift(worktree_workspace: Path) -> None:
    project = next(
        p for p in find_projects(worktree_workspace) if p.name.endswith("-feature")
    )
    row = _format_project_row(project, load_git_info(project.path))
    branch_line = row.splitlines()[1]
    assert "feat/thing" in branch_line
    assert "↑1" in branch_line


def test_a_project_without_git_gets_a_single_line_row(workspace: Path) -> None:
    project = find_projects(workspace)[0]
    assert _format_project_row(project, None) == "gimle-asgard  [dim]2 active[/]"


@pytest.mark.asyncio
async def test_the_subtitle_does_not_wait_for_the_git_scan(
    worktree_workspace: Path,
) -> None:
    """Grouping reads `.git` pointer files, so the counts are known at once."""
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test():
        assert "2 repos" in app.sub_title
        assert "1 worktree" in app.sub_title


@pytest.mark.asyncio
async def test_project_rows_are_repainted_after_the_scan(
    worktree_workspace: Path,
) -> None:
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 0
        await pilot.press("space")
        await pilot.pause()
        labels = [str(label.render()) for label in app.query(Label)]
        assert any("feat/thing" in text for text in labels)


@pytest.mark.asyncio
async def test_summary_pane_renders_the_git_section(worktree_workspace: Path) -> None:
    """The feature has to reach the pane, not just be computable."""
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 0
        await pilot.press("space")  # unfold gimle-asgard
        await pilot.pause()
        app.query_one(TaskListView).index = 1  # its worktree
        await pilot.pause()

        source = app.query_one(Markdown).source
        assert "## Worktree" in source
        assert "feat/thing" in source
        assert "worktree of `gimle-asgard`" in source
        assert "**Created** 20" in source  # a real timestamp, not "unknown"
        assert "- Add the feature" in source
        # The tasks are still there, and the hint sits below everything.
        assert "## Active tasks" in source
        assert source.rstrip().endswith("*Press `→` or `Enter` to open.*")


@pytest.mark.asyncio
async def test_summary_pane_has_no_git_section_without_git(workspace: Path) -> None:
    projects = find_projects(workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        source = app.query_one(Markdown).source
        assert "## Worktree" not in source
        assert "## Repository" not in source


# --- grouping worktrees under their repository ------------------------------


def test_worktrees_nest_under_their_repository(worktree_workspace: Path) -> None:
    groups = group_projects(find_projects(worktree_workspace))
    names = {g.project.name: [w.name for w in g.worktrees] for g in groups}
    assert names["gimle-asgard"] == ["gimle-asgard-feature"]
    # The worktree is not also a top-level entry.
    assert "gimle-asgard-feature" not in names


def test_a_project_without_worktrees_is_its_own_group(worktree_workspace: Path) -> None:
    groups = group_projects(find_projects(worktree_workspace))
    mimir = next(g for g in groups if g.project.name == "gimle-mimir")
    assert mimir.worktrees == []
    assert mimir.has_worktrees is False


def test_a_worktree_whose_repo_is_not_listed_stays_top_level(
    workspace: Path,
) -> None:
    """Its repository has no tasks folder, so it never reaches the list."""
    repo = workspace.parent / "outside-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Initial commit")

    detached = workspace / "outside-repo-feature"
    _git(repo, "worktree", "add", "-b", "feat/thing", str(detached))
    (detached / "tasks" / "open").mkdir(parents=True)
    (detached / "tasks" / "open" / "001-a.md").write_text("# A\n")

    groups = group_projects(find_projects(workspace))
    assert "outside-repo-feature" in [g.project.name for g in groups]


# --- the collapsible sidebar -----------------------------------------------


@pytest.mark.asyncio
async def test_worktrees_are_hidden_until_the_repo_is_expanded(
    worktree_workspace: Path,
) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        list_view = app.query_one(TaskListView)
        # gimle-asgard (with its worktree folded away) and gimle-mimir.
        assert len(list_view) == 2

        list_view.index = 0
        await pilot.press("space")
        await pilot.pause()
        assert len(list_view) == 3
        assert any("feat/thing" in str(label.render()) for label in app.query(Label))

        await pilot.press("space")
        await pilot.pause()
        assert len(list_view) == 2


@pytest.mark.asyncio
async def test_a_collapsed_repo_says_what_it_is_hiding(
    worktree_workspace: Path,
) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("1 wt" in row for row in rows)


@pytest.mark.asyncio
async def test_entering_an_expanded_worktree_opens_that_worktree(
    worktree_workspace: Path,
) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.query_one(TaskListView).index = 0
        await pilot.press("space")
        await pilot.pause()
        app.query_one(TaskListView).index = 1  # the worktree row
        await pilot.press("right")
        await pilot.pause()
        assert app._current_project.name == "gimle-asgard-feature"


@pytest.mark.asyncio
async def test_toggling_a_worktree_row_folds_its_repo(
    worktree_workspace: Path,
) -> None:
    """space on a child collapses the group it belongs to, not nothing."""
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        list_view = app.query_one(TaskListView)
        list_view.index = 0
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 1
        await pilot.press("space")
        await pilot.pause()
        assert len(list_view) == 2


@pytest.mark.asyncio
async def test_subtitle_counts_repos_and_worktrees(worktree_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "2 repos" in app.sub_title
        assert "1 worktree" in app.sub_title


@pytest.mark.asyncio
async def test_toggle_is_hidden_inside_a_project(worktree_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        assert app.check_action("toggle_group", ()) is True
        await pilot.press("right")
        await pilot.pause()
        assert app.check_action("toggle_group", ()) is None
