"""The owner's work queue: the ``next:`` field in a task's frontmatter.

``next`` is a running order the repo owner sets, deliberately separate from
``priority``. Priority says how important a task is; ``next`` says what to do
first. Lower ranks are taken first, gaps are fine, and **agents never write this
field** — see ``gimle-skills/references/task-format.md``.

Frontmatter is edited with a targeted line replacement rather than a YAML
round-trip, to preserve the author's formatting. That makes the matching rules
load-bearing: the key is only recognised at column zero, so a nested ``next:``
under some other mapping, or one inside a block scalar, is left alone. Writes go
through a temporary file and only replace the original if it has not changed
since it was read, because these files are edited concurrently — the background
groom pass and the `grind` agent both touch them.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .discovery import Task, load_tasks, ordered_fragments

# Ranks come from the tasks still in play; a closed task keeps its number but
# stops counting, so finishing one never forces a renumber.
_QUEUED_STATES = ("open", "ongoing")

# Tolerates CRLF, and matches discovery's idea of a frontmatter block.
_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A(---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*\r?\n?)", re.DOTALL
)

# Column zero only, and case-sensitive: YAML keys are, and `  next:` two spaces
# in is a *different* key belonging to some other mapping. Anchoring loosely
# would rewrite nested keys and cut block scalars in half.
_NEXT_LINE_RE = re.compile(r"^next:[ \t]*.*(?:\r?\n|\Z)", re.MULTILINE)


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
    _apply([(task, rank)], tasks_dir)
    return rank


def promote(task: Task, tasks_dir: Path) -> int:
    """Move ``task`` to the front of the queue. Returns its rank.

    Takes the slot below the current leader when there is one, so promoting
    usually rewrites a single file. Only a queue already starting at rank 1
    forces the others down.
    """
    others = [
        other
        for other in _queued(tasks_dir)
        if other.next_rank is not None and other.path != task.path
    ]
    if not others:
        _apply([(task, 1)], tasks_dir)
        return 1

    leader = min(other.next_rank or 0 for other in others)
    if leader > 1:
        _apply([(task, leader - 1)], tasks_dir)
        return leader - 1

    plan = [(other, (other.next_rank or 0) + 1) for other in others]
    plan.append((task, 1))
    _apply(plan, tasks_dir)
    return 1


def clear_next(task: Task, tasks_dir: Path | None = None) -> None:
    """Take ``task`` out of the queue, removing the field entirely."""
    md_file = _metadata_file(task)
    if md_file is None:
        return  # Nothing carrying a rank, so nothing to clear.
    original = _read(md_file)
    block = _FRONTMATTER_BLOCK_RE.match(original)
    if block is None:
        return
    body = _NEXT_LINE_RE.sub("", block.group(2) + "\n", count=1).rstrip("\n")
    updated = block.group(1) + body + block.group(3) + original[block.end():]
    if updated != original:
        _replace(md_file, updated, original)


def _apply(plan: list[tuple[Task, int]], tasks_dir: Path) -> None:
    """Write every rank in ``plan``, or none of them.

    Each target is validated first — it exists, it has frontmatter, it is
    writable — so a queue is never left half-renumbered by a file that was
    always going to refuse.
    """
    prepared: list[tuple[Path, str, str]] = []
    for task, rank in plan:
        if task.state not in _QUEUED_STATES:
            raise QueueError(f"{task.task_id} is {task.state}; only open or ongoing tasks queue")
        md_file = _metadata_file(task)
        if md_file is None:
            raise QueueError(f"{task.task_id} has no markdown to edit")
        original = _read(md_file)
        block = _FRONTMATTER_BLOCK_RE.match(original)
        if block is None:
            raise QueueError(
                f"{task.task_id} has no YAML frontmatter to hold a queue rank"
            )
        if not os.access(md_file, os.W_OK):
            raise QueueError(f"{task.task_id} is not writable")
        prepared.append((md_file, _with_rank(block, original, rank), original))

    for md_file, updated, original in prepared:
        _replace(md_file, updated, original)

    _verify(plan, tasks_dir)


def _with_rank(block: re.Match[str], original: str, rank: int) -> str:
    """The file's text with ``next:`` set to ``rank``, inserted if absent."""
    open_fence, body, close_fence = block.group(1), block.group(2), block.group(3)
    newline = "\r\n" if "\r\n" in open_fence else "\n"
    if _NEXT_LINE_RE.search(body + newline):
        body = _NEXT_LINE_RE.sub(
            f"next: {rank}{newline}", body + newline, count=1
        ).rstrip("\r\n")
    else:
        body = f"{body}{newline}next: {rank}"
    return open_fence + body + close_fence + original[block.end():]


def _verify(plan: list[tuple[Task, int]], tasks_dir: Path) -> None:
    """Confirm the loader now sees what we just wrote.

    A rank written where the loader does not read it — the wrong fragment of a
    directory task, say — would otherwise be reported as a success.
    """
    if tasks_dir is None:
        return
    seen = {t.path: t.next_rank for t in load_tasks(tasks_dir, _QUEUED_STATES)}
    for task, rank in plan:
        if seen.get(task.path) != rank:
            raise QueueError(
                f"{task.task_id}: wrote rank {rank} but it did not take effect"
            )


def _queued(tasks_dir: Path) -> list[Task]:
    return load_tasks(tasks_dir, _QUEUED_STATES)


def _ranks_in_play(tasks_dir: Path) -> list[int]:
    return [t.next_rank for t in _queued(tasks_dir) if t.next_rank is not None]


def _metadata_file(task: Task) -> Path | None:
    """The file whose frontmatter the loader actually reads.

    Follows the loader's own fragment precedence, so a rank is never written to
    a fragment it will not be read back from.
    """
    if task.path.is_file():
        return task.path
    for fragment in ordered_fragments(task.path):
        if _FRONTMATTER_BLOCK_RE.match(_read(fragment)):
            return fragment
    return None


def _read(path: Path) -> str:
    """Read strictly, preserving line endings.

    Decoding with ``errors="replace"`` and writing back would turn any byte that
    is not valid UTF-8 into a permanent U+FFFD.
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as error:
        raise QueueError(f"{path.name} is not valid UTF-8; refusing to edit it") from error


def _replace(path: Path, updated: str, expected: str) -> None:
    """Swap in ``updated``, but only if the file still holds ``expected``.

    Writing in place would truncate the original before the new content lands —
    a full disk then leaves a shredded task file. And these files have other
    writers: the groom pass and `grind` both edit them, so a stale write would
    silently drop someone else's claim.
    """
    if _read(path) != expected:
        raise QueueError(f"{path.name} changed on disk — reload and try again")
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(updated)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
