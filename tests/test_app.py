"""Headless smoke tests driving the TUI with Textual's Pilot."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.widgets import Markdown

from task_viewer.app import TaskListView, TaskViewerApp


@pytest.mark.asyncio
async def test_app_lists_tasks_and_renders_selection(project: Path) -> None:
    app = TaskViewerApp(project / "tasks", "gimle-example")
    async with app.run_test() as pilot:
        list_view = app.query_one(TaskListView)
        # Active (open + ongoing) by default: 3 open tasks in the fixture.
        assert len(list_view) == 3
        # First task's markdown is rendered on mount.
        assert app.query_one(Markdown) is not None

        # Toggling closed brings in the closed task.
        await pilot.press("o")
        assert len(list_view) == 4

        # Moving the cursor updates the shown task without error.
        await pilot.press("j")
        await pilot.press("k")


@pytest.mark.asyncio
async def test_mark_ongoing_then_done_moves_task(project: Path) -> None:
    tasks_dir = project / "tasks"
    app = TaskViewerApp(tasks_dir, "gimle-example")
    async with app.run_test() as pilot:
        list_view = app.query_one(TaskListView)
        list_view.index = 0  # first active task
        await pilot.pause()

        await pilot.press("g")  # mark ongoing
        await pilot.pause()
        assert any((tasks_dir / "ongoing").glob("*")), "task should move to ongoing/"

        await pilot.press("x")  # mark done
        await pilot.pause()
        # It left the active view (open + ongoing); closed toggle reveals it.
        await pilot.press("o")
        await pilot.pause()
        assert any((tasks_dir / "closed").glob("*"))


@pytest.mark.asyncio
async def test_background_groom_runs_and_shows_report(
    project: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake_claude.py"
    stub.write_text(
        "import os, sys\n"
        "open(os.path.join(os.getcwd(), 'GROOMED'), 'w').close()\n"
        "sys.stdout.write('- 052: raised priority to high\\n')\n"
    )
    app = TaskViewerApp(
        project / "tasks", "gimle-example", groom_cmd=[sys.executable, str(stub)]
    )
    async with app.run_test() as pilot:
        await pilot.press("R")
        await app.workers.wait_for_complete()
        await pilot.pause()
        # The agent ran in the project root...
        assert (project / "GROOMED").exists()
        # ...and the app returned to a non-grooming state.
        assert app._grooming is False
        assert "reviewing" not in app.sub_title


@pytest.mark.asyncio
async def test_tab_switches_focus(project: Path) -> None:
    app = TaskViewerApp(project / "tasks", "gimle-example")
    async with app.run_test() as pilot:
        assert isinstance(app.focused, TaskListView)
        await pilot.press("tab")
        assert not isinstance(app.focused, TaskListView)
