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
    assert "+1" in branch_line


def test_a_project_without_git_gets_a_single_line_row(workspace: Path) -> None:
    project = find_projects(workspace)[0]
    assert _format_project_row(project, None) == "  gimle-asgard  [dim]2 active[/]"


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
        await pilot.pause()
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
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        app.query_one(TaskListView).index = 1  # the worktree row
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 1
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        assert len(list_view) == 2


@pytest.mark.asyncio
async def test_subtitle_counts_repos_and_worktrees(worktree_workspace: Path) -> None:
    """Grouping reads `.git` pointer files, so the counts need no git command."""
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        assert "2 repos" in app.sub_title
        assert "1 worktree" in app.sub_title
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "2 repos" in app.sub_title


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
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 2  # the worktree
        await pilot.pause()
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
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("left")
        await app.workers.wait_for_complete()
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
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        list_view.index = 2
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("left")
        # Stepping back out starts a fresh scan, which repaints the rows when it
        # lands; assert on the settled list, not on it mid-rebuild.
        await app.workers.wait_for_complete()
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


@pytest.fixture
def tracking_workspace(tmp_path: Path) -> Path:
    """A workspace with one project whose origin has a task it has not seen."""
    origin = tmp_path / "origin"
    (origin / "tasks" / "open").mkdir(parents=True)
    (origin / "tasks" / "open" / "001-a.md").write_text("# A\n")
    _git(origin, "init", "-b", "main")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "Initial commit")

    workspace = tmp_path / "gimle"
    workspace.mkdir()
    _git(workspace, "clone", "--quiet", str(origin), str(workspace / "proj"))

    (origin / "tasks" / "open" / "099-new-task.md").write_text("# Newly filed\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "File a new task")
    return workspace


@pytest.mark.asyncio
async def test_launching_notices_the_remote_has_moved(
    tracking_workspace: Path,
) -> None:
    """No keypress: the startup fetch is what makes a stale task list visible."""
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("↓1" in row for row in rows)


@pytest.mark.asyncio
async def test_f_picks_up_a_commit_that_landed_after_launch(
    tracking_workspace: Path, tmp_path: Path
) -> None:
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        (tmp_path / "origin" / "tasks" / "open" / "100-later.md").write_text("# Later\n")
        _git(tmp_path / "origin", "add", "-A")
        _git(tmp_path / "origin", "commit", "-m", "File another")

        await pilot.press("f")
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("↓2" in row for row in rows)


@pytest.mark.asyncio
async def test_update_brings_in_the_tasks_and_repaints_the_row(
    tracking_workspace: Path,
) -> None:
    """The commit's headline claim: the task list is stale too, so it reloads."""
    repo = tracking_workspace / "proj"
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert any("1 active" in str(l.render()) for l in app.query(Label))

        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert (repo / "tasks" / "open" / "099-new-task.md").exists()
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("2 active" in row for row in rows)
        assert not any("↓" in row for row in rows)


@pytest.mark.asyncio
async def test_a_worktree_is_not_fetched_separately_from_its_repo(
    worktree_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They share one object store; fetching both just repeats the round trip."""
    asked: list[Path] = []
    monkeypatch.setattr(
        "task_viewer.app.fetch", lambda path: asked.append(path) or True
    )
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert asked == [
        worktree_workspace / "gimle-asgard",
        worktree_workspace / "gimle-mimir",
    ]


@pytest.mark.asyncio
async def test_the_subtitle_says_it_is_fetching_from_the_start(
    tracking_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup fetch is the one path the indicator exists for."""
    monkeypatch.setattr("task_viewer.app.fetch", lambda path: True)
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        assert "fetching" in app.sub_title
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "fetching" not in app.sub_title


@pytest.mark.asyncio
async def test_an_unreachable_remote_is_admitted_to(
    tracking_workspace: Path, tmp_path: Path
) -> None:
    """A failed fetch still bumps FETCH_HEAD, so silence would read as freshness."""
    (tmp_path / "origin").rename(tmp_path / "origin-gone")
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        source = app.query_one(Markdown).source
        assert "could not reach the remote" in source
        assert "checked just now" not in source


@pytest.mark.asyncio
async def test_the_refusal_notification_is_a_warning_that_names_the_project(
    tracking_workspace: Path,
) -> None:
    (tracking_workspace / "proj" / "scratch.txt").write_text("wip\n")
    app = TaskViewerApp(find_projects(tracking_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("u")
        await app.workers.wait_for_complete()
        await pilot.pause()
        last = list(app._notifications)[-1]
        assert last.severity == "warning"
        assert last.message.startswith("proj: ")
        assert "uncommitted" in last.message


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


@pytest.mark.asyncio
async def test_a_rebuild_with_an_unknown_row_keeps_the_cursor_put(
    trailing_worktree_workspace: Path,
) -> None:
    """Falling back to row 0 would teleport the cursor on any background scan."""
    projects = find_projects(trailing_worktree_workspace)
    app = TaskViewerApp(projects, "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        app.query_one(TaskListView).index = 2
        await pilot.pause()

        app._build_project_rows(keep=Path("/nowhere/at/all"))
        await pilot.pause()
        assert app.query_one(TaskListView).index == 2


# --- pull requests, per worktree --------------------------------------------


@pytest.fixture
def pr_workspace(worktree_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """The worktree's branch has an open PR; gh is stubbed, nothing dials out."""
    from task_viewer.pull_requests import Check, Comment, Listing, PullRequest

    pull = PullRequest(
        number=453,
        title="Task 140: derivative features",
        body="## Summary\nAdds derivative features.",
        url="https://github.com/arnovich/gimle-asgard/pull/453",
        branch="feat/thing",
        mergeable="MERGEABLE",
        changed_files=8,
        additions=625,
        deletions=41,
        checks=[Check("test", "SUCCESS"), Check("lint", "SUCCESS")],
        comments=[
            Comment("erikarne", "Add the benchmark."),
            Comment("github-actions[bot]", "Deploy preview ready"),
        ],
    )
    monkeypatch.setattr(
        "task_viewer.app.load_pull_requests",
        lambda root: Listing({"feat/thing": pull}) if root.name == "gimle-asgard"
        else Listing({}),
    )
    return worktree_workspace


async def _open_expanded(app, pilot):
    await app.workers.wait_for_complete()
    app.query_one(TaskListView).index = 0
    await pilot.pause()
    await pilot.press("space")
    await pilot.pause()
    app.query_one(TaskListView).index = 1  # the worktree
    await pilot.pause()


@pytest.mark.asyncio
async def test_a_worktree_row_shows_its_pr_number(pr_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("#453" in row for row in rows)


@pytest.mark.asyncio
async def test_the_pane_shows_the_description_and_human_comments(
    pr_workspace: Path,
) -> None:
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        source = app.query_one(Markdown).source
        assert "## Pull request #453" in source
        assert "Adds derivative features." in source
        assert "**erikarne** — Add the benchmark." in source
        assert "Deploy preview ready" not in source  # bots stay out
        assert "2/2 passing" in source


@pytest.fixture
def merges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    """Record merge attempts instead of performing them."""
    from task_viewer.pull_requests import ActionResult

    recorded: list[tuple[int, bool]] = []

    def fake_merge(root: Path, number: int, admin: bool = False) -> ActionResult:
        recorded.append((number, admin))
        return ActionResult(True, f"merged #{number}")

    monkeypatch.setattr("task_viewer.app.merge_pull_request", fake_merge)
    return recorded


@pytest.mark.asyncio
async def test_merge_asks_before_it_merges(
    pr_workspace: Path, merges: list
) -> None:
    """Merging is outward-facing; it must never be a single keypress."""
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)

        await pilot.press("M")
        await pilot.pause()
        assert merges == []  # the dialog is up; nothing has happened yet

        await pilot.press("escape")
        await pilot.pause()
        assert merges == []  # cancelled outright

        await pilot.press("M")
        await pilot.pause()
        await pilot.press("y")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert merges == [(453, False)]


@pytest.mark.asyncio
async def test_a_blocked_pr_says_so_before_you_merge_it(
    pr_workspace: Path, merges: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing stops you merging a red PR — but you are told first."""
    from task_viewer.pull_requests import PullRequest

    from task_viewer.pull_requests import Check, Listing

    failing = PullRequest(
        number=453, title="t", body="", url="u", branch="feat/thing",
        mergeable="MERGEABLE",
        checks=[Check("test", "FAILURE"), Check("lint", "SUCCESS")],
    )
    monkeypatch.setattr(
        "task_viewer.app.load_pull_requests",
        lambda root: Listing({"feat/thing": failing}) if root.name == "gimle-asgard"
        else Listing({}),
    )
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        assert "test failing" in app.query_one(Markdown).source

        await pilot.press("M")
        await pilot.pause()
        # The dialog is a separate screen, so query it, not the app.
        shown = " ".join(str(label.render()) for label in app.screen.query(Label))
        assert "test" in shown  # it names the check that went red
        await pilot.press("y")
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert merges == [(453, False)]


@pytest.mark.asyncio
async def test_the_pr_keys_are_hidden_without_a_pr(worktree_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.check_action("merge_pr", ()) is None
        assert app.check_action("comment_pr", ()) is None


# --- the comment path, which is the only channel to the agents --------------


@pytest.fixture
def editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a fake $EDITOR that writes a fixed body (or fails)."""

    def install(body: str | None, exit_code: int = 0):
        script = tmp_path / "fake-editor"
        write = (
            f"pathlib.Path(sys.argv[1]).write_text({body!r}, encoding='utf-8')"
            if body is not None
            else "pass"
        )
        script.write_text(
            f"#!/usr/bin/env python3\nimport sys, pathlib\n{write}\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        monkeypatch.setenv("EDITOR", str(script))
        monkeypatch.delenv("VISUAL", raising=False)

    return install


def _posted(monkeypatch: pytest.MonkeyPatch, ok: bool = True) -> list:
    from task_viewer.pull_requests import ActionResult

    seen: list = []

    def fake(root: Path, number: int, body: str) -> ActionResult:
        seen.append((number, body))
        return ActionResult(ok, f"commented on #{number}" if ok else "gh refused")

    monkeypatch.setattr("task_viewer.app.post_comment", fake)
    return seen


@pytest.mark.asyncio
async def test_a_comment_keeps_every_line_the_user_wrote(
    pr_workspace: Path, editor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`## Blocking` is a heading and `#538` is a PR — not commentary."""
    written = "Looks good, two things:\n\n## Blocking\n#538 supersedes this.\n"
    editor(written)
    posted = _posted(monkeypatch)

    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        note = list(app._notifications)[-1].message if app._notifications else "(none)"

    assert posted, f"nothing was posted; last notification: {note}"
    number, body = posted[0]
    assert number == 453
    assert "## Blocking" in body
    assert "#538 supersedes this." in body
    assert "tv:" not in body  # the template fence is gone


@pytest.mark.asyncio
async def test_an_editor_that_aborts_posts_nothing(
    pr_workspace: Path, editor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`vim :cq` is the conventional abort and exits non-zero."""
    editor("half a thought", exit_code=1)
    posted = _posted(monkeypatch)

    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("m")
        await pilot.pause()
    assert posted == []


@pytest.mark.asyncio
async def test_an_untouched_template_posts_nothing(
    pr_workspace: Path, editor, monkeypatch: pytest.MonkeyPatch
) -> None:
    editor(None)  # editor writes nothing at all
    posted = _posted(monkeypatch)

    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("m")
        await pilot.pause()
    assert posted == []


@pytest.mark.asyncio
async def test_a_failed_post_keeps_the_draft(
    pr_workspace: Path, editor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review is expensive to write; an expired token must not eat it."""
    editor("A long, carefully written review.")
    _posted(monkeypatch, ok=False)

    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("m")
        await app.workers.wait_for_complete()
        await pilot.pause()
        message = list(app._notifications)[-1].message

    assert "draft kept at" in message
    kept = Path(message.split("draft kept at ")[1].strip())
    assert kept.exists()
    assert "carefully written review" in kept.read_text(encoding="utf-8")
    kept.unlink()


@pytest.mark.asyncio
async def test_a_conflicted_pr_is_not_offered_a_dead_end_merge(
    pr_workspace: Path, merges: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub refuses a conflicted merge, and --admin does not change that."""
    from task_viewer.pull_requests import Listing, PullRequest

    conflicted = PullRequest(
        number=453, title="t", body="", url="u", branch="feat/thing",
        mergeable="CONFLICTING",
    )
    monkeypatch.setattr(
        "task_viewer.app.load_pull_requests",
        lambda root: Listing({"feat/thing": conflicted})
        if root.name == "gimle-asgard" else Listing({}),
    )
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        assert "cannot merge" in app.query_one(Markdown).source

        await pilot.press("M")
        await pilot.pause()
        assert merges == []  # no dialog, no dead-end keypress
        assert "cannot be merged" in list(app._notifications)[-1].message


@pytest.mark.asyncio
async def test_a_title_with_markup_neither_crashes_nor_is_rewritten(
    pr_workspace: Path, merges: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `[WIP]` prefix must survive into the dialog that asks about it."""
    from task_viewer.pull_requests import Listing, PullRequest

    marked = PullRequest(
        number=453, title="[WIP] Close [/] the stream", body="", url="u",
        branch="feat/thing", mergeable="MERGEABLE",
    )
    monkeypatch.setattr(
        "task_viewer.app.load_pull_requests",
        lambda root: Listing({"feat/thing": marked})
        if root.name == "gimle-asgard" else Listing({}),
    )
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("M")
        await pilot.pause()
        assert app.is_running
        shown = " ".join(str(label.render()) for label in app.screen.query(Label))
        assert "[WIP]" in shown
        await pilot.press("escape")
        await pilot.pause()


@pytest.mark.asyncio
async def test_a_collapsed_repo_says_a_pr_is_waiting(pr_workspace: Path) -> None:
    """Otherwise the only way to learn is to expand every group and look."""
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        rows = [str(label.render()) for label in app.query(Label)]
        assert any("1 PR" in row for row in rows)
        assert "1 PR" in app.sub_title


@pytest.mark.asyncio
async def test_gh_being_unavailable_is_not_an_empty_queue(
    worktree_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from task_viewer.pull_requests import Listing

    monkeypatch.setattr(
        "task_viewer.app.load_pull_requests",
        lambda root: Listing(reached=False, error="gh: not found"),
    )
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "gh unavailable" in app.sub_title


@pytest.mark.asyncio
async def test_w_opens_the_pr_in_a_browser(
    pr_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    from task_viewer.pull_requests import ActionResult

    monkeypatch.setattr(
        "task_viewer.app.open_in_browser",
        lambda url: opened.append(url) or ActionResult(True, f"opened {url}"),
    )
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        await pilot.press("w")
        await pilot.pause()
    assert opened == ["https://github.com/arnovich/gimle-asgard/pull/453"]


@pytest.mark.asyncio
async def test_clicking_the_link_in_the_pane_opens_it(
    pr_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL was text you had to select with a mouse; make it a link."""
    opened: list[str] = []
    from task_viewer.pull_requests import ActionResult

    monkeypatch.setattr(
        "task_viewer.app.open_in_browser",
        lambda url: opened.append(url) or ActionResult(True, "opened"),
    )
    app = TaskViewerApp(find_projects(pr_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await _open_expanded(app, pilot)
        app.post_message(
            Markdown.LinkClicked(
                app.query_one(Markdown), "https://github.com/arnovich/x/pull/9"
            )
        )
        await pilot.pause()
    assert opened == ["https://github.com/arnovich/x/pull/9"]


@pytest.mark.asyncio
async def test_w_does_nothing_without_a_pr(worktree_workspace: Path) -> None:
    app = TaskViewerApp(find_projects(worktree_workspace), "gimle", workspace=True)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.check_action("open_pr", ()) is None
