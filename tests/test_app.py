"""Headless smoke tests driving the TUI with Textual's Pilot."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Markdown

from task_viewer.app import TaskListView, TaskViewerApp


@pytest.mark.asyncio
async def test_app_lists_tasks_and_renders_selection(project: Path) -> None:
    app = TaskViewerApp(project / "tasks", "gimle-example")
    async with app.run_test() as pilot:
        list_view = app.query_one(TaskListView)
        # Open-only by default: 3 open tasks in the fixture.
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
async def test_tab_switches_focus(project: Path) -> None:
    app = TaskViewerApp(project / "tasks", "gimle-example")
    async with app.run_test() as pilot:
        assert isinstance(app.focused, TaskListView)
        await pilot.press("tab")
        assert not isinstance(app.focused, TaskListView)
