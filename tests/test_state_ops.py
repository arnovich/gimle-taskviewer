"""Tests for moving tasks between states and syncing frontmatter."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.discovery import load_tasks
from task_viewer.state_ops import StateChangeError, set_state


def _task(project: Path, task_id: str, states=("open", "ongoing", "closed")):
    tasks = {t.task_id: t for t in load_tasks(project / "tasks", states)}
    return tasks[task_id]


def test_set_state_moves_file_and_updates_frontmatter(project: Path) -> None:
    task = _task(project, "052-heat-equation")
    new_path = set_state(task, "ongoing", project / "tasks")

    assert new_path == project / "tasks" / "ongoing" / "052-heat-equation.md"
    assert new_path.exists()
    assert not (project / "tasks" / "open" / "052-heat-equation.md").exists()
    assert "state: ongoing" in new_path.read_text()


def test_set_state_preserves_other_frontmatter(project: Path) -> None:
    task = _task(project, "052-heat-equation")
    new_path = set_state(task, "closed", project / "tasks")
    text = new_path.read_text()
    assert 'title: "2D heat equation"' in text
    assert "labels: [enhancement, runtime]" in text
    assert "state: closed" in text


def test_set_state_roundtrip_open_ongoing_open(project: Path) -> None:
    task = _task(project, "052-heat-equation")
    set_state(task, "ongoing", project / "tasks")
    task = _task(project, "052-heat-equation")
    assert task.state == "ongoing"
    back = set_state(task, "open", project / "tasks")
    assert back.parent.name == "open"
    assert "state: open" in back.read_text()


def test_set_state_inserts_state_when_missing(project: Path) -> None:
    # 003 has no frontmatter at all; moving it should not crash, and the folder
    # remains the source of truth.
    task = _task(project, "003-no-frontmatter")
    new_path = set_state(task, "ongoing", project / "tasks")
    assert new_path.parent.name == "ongoing"


def test_set_state_directory_task(project: Path) -> None:
    task = _task(project, "010-dir-task")
    new_path = set_state(task, "ongoing", project / "tasks")
    assert new_path.is_dir()
    assert (new_path / "plan.md").exists()
    assert "state: ongoing" in (new_path / "description.md").read_text()


def test_set_state_rejects_unknown_state(project: Path) -> None:
    task = _task(project, "052-heat-equation")
    with pytest.raises(StateChangeError):
        set_state(task, "archived", project / "tasks")
