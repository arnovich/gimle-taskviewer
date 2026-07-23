"""Tests for locating and loading tasks."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.discovery import (
    TasksNotFoundError,
    find_tasks_dir,
    load_tasks,
)


def test_find_tasks_dir_walks_up(project: Path) -> None:
    nested = project / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_tasks_dir(nested) == project / "tasks"


def test_find_tasks_dir_raises_when_absent(tmp_path: Path) -> None:
    with pytest.raises(TasksNotFoundError):
        find_tasks_dir(tmp_path)


def test_find_tasks_dir_custom_folder_name(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "issues" / "open").mkdir(parents=True)
    (root / "issues" / "open" / "001-thing.md").write_text("# Thing\n")
    assert find_tasks_dir(root, "issues") == root / "issues"
    # Default name is not found when the folder is called something else.
    with pytest.raises(TasksNotFoundError):
        find_tasks_dir(root)


def test_load_open_only(project: Path) -> None:
    tasks = load_tasks(project / "tasks", ("open",))
    assert all(t.state == "open" for t in tasks)
    ids = [t.task_id for t in tasks]
    # Sorted by leading number: 003, 010, 052.
    assert ids == ["003-no-frontmatter", "010-dir-task", "052-heat-equation"]


def test_load_includes_closed(project: Path) -> None:
    tasks = load_tasks(project / "tasks", ("open", "closed"))
    assert any(t.state == "closed" for t in tasks)
    assert tasks[0].task_id == "001-done"  # lowest number, regardless of state


def test_frontmatter_title_and_labels(project: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(project / "tasks", ("open",))}
    heat = tasks["052-heat-equation"]
    assert heat.title == "2D heat equation"
    assert heat.labels == ["enhancement", "runtime"]
    assert heat.priority == "low"
    assert "Some body text." in heat.body


def test_title_falls_back_to_heading(project: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(project / "tasks", ("open",))}
    assert tasks["003-no-frontmatter"].title == "Plain task"


def test_directory_task_concatenates_fragments(project: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(project / "tasks", ("open",))}
    dir_task = tasks["010-dir-task"]
    assert dir_task.title == "Directory task"
    assert dir_task.labels == ["multi"]
    assert "Description part." in dir_task.body
    assert "The plan part." in dir_task.body
