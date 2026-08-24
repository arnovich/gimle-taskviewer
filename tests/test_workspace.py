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
    assert _format_project_row(project, None) == "  gimle-asgard  [dim]2 active[/]"


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
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_rows_gain_their_git_line_when_the_scan_lands(
    worktree_workspace: Path,
) -> None:
    """No keypress: the row paints bare, then fills in on its own."""
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        before = [str(label.render()) for label in app.query(Label)]
        assert not any("⎇" in text for text in before)

        await app.workers.wait_for_complete()
        await pilot.pause()
        after = [str(label.render()) for label in app.query(Label)]
        assert any("⎇ main" in text for text in after)


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
        assert any("feature" in str(label.render()) for label in app.query(Label))

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


# --- where the cursor lands -------------------------------------------------


@pytest.fixture
def trailing_worktree_workspace(worktree_workspace: Path) -> Path:
    """A workspace where the repo with worktrees is NOT the first row.

    With it first, "landed on the repo" and "reset to row 0" are the same
    answer, and the tests below cannot tell a bug from correct behaviour.
    """
    _make_project(worktree_workspace, "aaa-plain", ["001-x"])
    return worktree_workspace


def _highlighted(app: TaskViewerApp) -> list[int]:
    list_view = app.query_one(TaskListView)
    return [i for i, node in enumerate(list_view._nodes) if node.highlighted]


@pytest.mark.asyncio
async def test_the_cursor_stays_visible_after_folding(
    trailing_worktree_workspace: Path,
) -> None:
    """ListView.clear() prunes asynchronously; the highlight must survive it."""
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 1  # gimle-asgard, which has a worktree
        await pilot.press("space")
        await pilot.pause()
        assert _highlighted(app) == [1]


@pytest.mark.asyncio
async def test_folding_from_a_worktree_row_lands_on_its_repo(
    trailing_worktree_workspace: Path,
) -> None:
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        list_view = app.query_one(TaskListView)
        list_view.index = 1
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 2  # the worktree
        await pilot.press("space")
        await pilot.pause()
        assert list_view.index == 1
        assert _highlighted(app) == [1]
        assert app.query_one(Markdown).source.startswith("# gimle-asgard\n")


@pytest.mark.asyncio
async def test_back_from_a_project_reselects_it(
    trailing_worktree_workspace: Path,
) -> None:
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 2  # gimle-mimir
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert app.query_one(TaskListView).index == 2
        assert _highlighted(app) == [2]


@pytest.mark.asyncio
async def test_back_from_a_worktree_keeps_its_repo_unfolded(
    trailing_worktree_workspace: Path,
) -> None:
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        list_view = app.query_one(TaskListView)
        list_view.index = 1
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 2
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert len(list_view) == 4  # still unfolded
        assert list_view.index == 2
        assert _highlighted(app) == [2]


@pytest.mark.asyncio
async def test_the_first_keypress_is_not_swallowed(worktree_workspace: Path) -> None:
    """`→` must work immediately, before any refresh has settled."""
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await pilot.press("right")
        await pilot.pause()
        assert app._level == "tasks"
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_space_does_nothing_on_a_repo_without_worktrees(
    trailing_worktree_workspace: Path,
) -> None:
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 0  # aaa-plain
        await pilot.pause()
        before = [str(label.render()) for label in app.query(Label)]
        assert app.check_action("toggle_group", ()) is None  # not advertised
        await pilot.press("space")
        await pilot.pause()
        assert [str(label.render()) for label in app.query(Label)] == before


@pytest.mark.asyncio
async def test_a_scan_landing_inside_a_project_leaves_the_task_list_alone(
    worktree_workspace: Path,
) -> None:
    projects = find_projects(worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await pilot.press("right")  # step in before the scan lands
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app._level == "tasks"
        labels = [str(label.render()) for label in app.query(Label)]
        assert not any("⎇" in text for text in labels)


# --- keeping up with the remote ---------------------------------------------


@pytest.mark.asyncio
async def test_update_fast_forwards_the_highlighted_project(
    worktree_workspace: Path, tmp_path: Path
) -> None:
    """Pressing `u` brings in the commits — and the tasks that rode with them."""
    repo = worktree_workspace / "gimle-asgard"
    origin = tmp_path / "origin"
    _git(repo, "clone", "--quiet", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "branch", "--set-upstream-to=origin/main", "main")

    # A task lands on the remote that this checkout has never seen.
    scratch = tmp_path / "scratch"
    _git(tmp_path, "clone", "--quiet", str(origin), str(scratch))
    (scratch / "tasks" / "open" / "099-new-task.md").write_text("# Newly filed\n")
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-m", "File a new task")
    _git(scratch, "push", "--quiet", "origin", "main")
    _git(repo, "fetch", "--quiet", "origin")

    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        assert not (repo / "tasks" / "open" / "099-new-task.md").exists()

        await pilot.press("u")
        await pilot.pause()
        assert (repo / "tasks" / "open" / "099-new-task.md").exists()
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_update_is_hidden_inside_a_project(worktree_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        assert app.check_action("update", ()) is True
        assert app.check_action("fetch", ()) is True
        await pilot.press("right")
        await pilot.pause()
        assert app.check_action("update", ()) is None
        assert app.check_action("fetch", ()) is None
        await app.workers.wait_for_complete()


@pytest.mark.asyncio
async def test_update_of_a_project_with_no_remote_changes_nothing(
    worktree_workspace: Path,
) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 0
        await pilot.pause()
        before = [str(label.render()) for label in app.query(Label)]
        await pilot.press("u")
        await pilot.pause()
        assert [str(label.render()) for label in app.query(Label)] == before
