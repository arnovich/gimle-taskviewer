"""Tests for the owner-set work queue (`next:` in a task's frontmatter)."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.discovery import load_tasks
from task_viewer.queue_ops import QueueError, clear_next, enqueue, promote


def _task(root: Path, task_id: str, extra: str = "") -> Path:
    path = root / "tasks" / "open" / f"{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: Task {task_id}\nstate: open\npriority: medium\n"
        f"labels: []\n{extra}---\n\n# Task {task_id}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def tasks_dir(tmp_path: Path) -> Path:
    _task(tmp_path, "001-a")
    _task(tmp_path, "002-b")
    _task(tmp_path, "003-c")
    return tmp_path / "tasks"


def _ranks(tasks_dir: Path) -> dict[str, int | None]:
    return {task.task_id: task.next_rank for task in load_tasks(tasks_dir)}


def test_a_task_starts_unqueued(tasks_dir: Path) -> None:
    assert _ranks(tasks_dir) == {"001-a": None, "002-b": None, "003-c": None}


def test_enqueue_appends_to_the_end_of_the_queue(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    enqueue(tasks["001-a"], tasks_dir)

    assert _ranks(tasks_dir) == {"001-a": 2, "002-b": 1, "003-c": None}


def test_enqueue_is_a_no_op_for_an_already_queued_task(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    again = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]
    enqueue(again, tasks_dir)

    assert _ranks(tasks_dir)["002-b"] == 1


def test_promote_puts_a_task_at_the_front(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["001-a"], tasks_dir)
    enqueue(tasks["002-b"], tasks_dir)
    third = {t.task_id: t for t in load_tasks(tasks_dir)}["003-c"]

    promote(third, tasks_dir)
    ranks = _ranks(tasks_dir)
    assert ranks["003-c"] < ranks["001-a"]
    assert ranks["003-c"] >= 1  # the standard says positive integers


def test_promote_renumbers_only_when_it_has_to(tasks_dir: Path) -> None:
    """Gaps are cheap; rewriting every queued file is not."""
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["001-a"], tasks_dir)  # rank 1, so there is no room below
    second = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]

    promote(second, tasks_dir)
    ranks = _ranks(tasks_dir)
    assert ranks["002-b"] == 1
    assert ranks["001-a"] == 2


def test_clear_removes_the_field_entirely(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    queued = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]

    clear_next(queued)
    assert _ranks(tasks_dir)["002-b"] is None
    assert "next:" not in queued.path.read_text(encoding="utf-8")


def test_clearing_an_unqueued_task_is_harmless(tasks_dir: Path) -> None:
    task = load_tasks(tasks_dir)[0]
    clear_next(task)
    assert _ranks(tasks_dir)[task.task_id] is None


def test_the_queue_ignores_closed_tasks(tmp_path: Path) -> None:
    """A finished task keeps its rank; it just stops counting."""
    _task(tmp_path, "001-a", extra="next: 1\n")
    closed = tmp_path / "tasks" / "closed"
    closed.mkdir(parents=True)
    (closed / "009-done.md").write_text(
        "---\ntitle: Done\nstate: closed\npriority: low\nlabels: []\nnext: 5\n---\n",
        encoding="utf-8",
    )
    tasks_dir = tmp_path / "tasks"
    task = _task(tmp_path, "002-b")
    enqueue(load_tasks(tasks_dir)[-1], tasks_dir)

    # 5 belongs to a closed task, so the next free rank is 2, not 6.
    assert _ranks(tasks_dir)["002-b"] == 2
    assert task.exists()


def test_a_task_without_frontmatter_cannot_be_queued(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "open" / "001-bare.md"
    path.parent.mkdir(parents=True)
    path.write_text("# No frontmatter here\n", encoding="utf-8")

    with pytest.raises(QueueError):
        enqueue(load_tasks(tmp_path / "tasks")[0], tmp_path / "tasks")
