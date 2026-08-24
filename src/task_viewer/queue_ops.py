"""The owner's work queue: the ``next:`` field in a task's frontmatter.

``next`` is a running order the repo owner sets, deliberately separate from
``priority``. Priority says how important a task is; ``next`` says what to do
first. Lower ranks are taken first, gaps are fine, and **agents never write this
field** — see ``gimle-skills/references/task-format.md``.

Frontmatter is edited with a targeted line replacement rather than a YAML
round-trip, to preserve the author's formatting — the same approach as
:mod:`state_ops`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .discovery import Task, load_tasks

# Ranks are taken from the tasks still in play; a closed task keeps its number
# but stops counting, so finishing one never forces a renumber.
_QUEUED_STATES = ("open", "ongoing")

_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---[ \t]*\n)(.*?)(\n---[ \t]*\n)", re.DOTALL)
_NEXT_LINE_RE = re.compile(r"^[ \t]*next:[ \t]*.*$\n?", re.IGNORECASE | re.MULTILINE)


class QueueError(Exception):
    """Raised when a task's queue rank cannot be changed."""


def enqueue(task: Task, tasks_dir: Path) -> int:
    """Put ``task`` at the end of the queue. Returns its rank.

    Already-queued tasks keep the rank they have — enqueueing twice should not
    quietly move a task.
    """
    if task.next_rank is not None:
        return task.next_rank
    rank = max(_ranks_in_play(tasks_dir), default=0) + 1
    _write_rank(task, rank)
    return rank


def promote(task: Task, tasks_dir: Path) -> int:
    """Move ``task`` to the front of the queue. Returns its rank.

    Takes the slot below the current leader when there is one, so promoting
    usually rewrites a single file. Only a queue already starting at rank 1
    forces everything else down.
    """
    others = {
        other.task_id: other.next_rank
        for other in _queued(tasks_dir)
        if other.task_id != task.task_id and other.next_rank is not None
    }
    if not others:
        _write_rank(task, 1)
        return 1

    leader = min(others.values())
    if leader > 1:
        _write_rank(task, leader - 1)
        return leader - 1

    for other in _queued(tasks_dir):
        if other.task_id != task.task_id and other.next_rank is not None:
            _write_rank(other, other.next_rank + 1)
    _write_rank(task, 1)
    return 1


def clear_next(task: Task) -> None:
    """Take ``task`` out of the queue, removing the field entirely."""
    for md_file in _markdown_files(task):
        text = md_file.read_text(encoding="utf-8", errors="replace")
        block = _FRONTMATTER_BLOCK_RE.match(text)
        if block is None:
            continue
        body = _NEXT_LINE_RE.sub("", block.group(2))
        updated = block.group(1) + body + block.group(3) + text[block.end():]
        if updated != text:
            md_file.write_text(updated, encoding="utf-8")


def _queued(tasks_dir: Path) -> list[Task]:
    return load_tasks(tasks_dir, _QUEUED_STATES)


def _ranks_in_play(tasks_dir: Path) -> list[int]:
    return [t.next_rank for t in _queued(tasks_dir) if t.next_rank is not None]


def _write_rank(task: Task, rank: int) -> None:
    """Set ``next:`` in the task's frontmatter, inserting the line if absent."""
    files = _markdown_files(task)
    if not files:
        raise QueueError(f"{task.task_id} has no markdown to edit")
    wrote = False
    for md_file in files:
        text = md_file.read_text(encoding="utf-8", errors="replace")
        block = _FRONTMATTER_BLOCK_RE.match(text)
        if block is None:
            continue
        open_fence, body, close_fence = block.group(1), block.group(2), block.group(3)
        if _NEXT_LINE_RE.search(body + "\n"):
            body = _NEXT_LINE_RE.sub(f"next: {rank}\n", body + "\n", count=1).rstrip("\n")
        else:
            body = f"{body}\nnext: {rank}"
        md_file.write_text(
            open_fence + body + close_fence + text[block.end():], encoding="utf-8"
        )
        wrote = True
        break  # A directory task carries its metadata in one fragment.
    if not wrote:
        raise QueueError(
            f"{task.task_id} has no YAML frontmatter to hold a queue rank"
        )


def _markdown_files(task: Task) -> list[Path]:
    if task.path.is_file():
        return [task.path]
    return sorted(task.path.glob("*.md"))

