"""Locate a project's ``tasks/`` folder and load its markdown task files.

A gimle project keeps tasks under ``tasks/open`` and ``tasks/closed``. A task is
either a single ``*.md`` file or a directory holding several markdown fragments
(``description.md``, ``spec.md``, ``plan.md`` ...). Both shapes are normalised
into a :class:`Task`.

Symbolic links are never followed, at any level: a task file is written by
agents and checked out by git, and a link is a way to make a viewer read — and
a writer publish — a file that was never in the repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .conversation import Entry, open_question, parse

# States map one-to-one onto the subdirectories of ``tasks/``, in lifecycle order.
STATES = ("open", "ongoing", "closed")

# Order to concatenate fragments of a directory-style task, most-relevant first.
_FRAGMENT_ORDER = ("description.md", "spec.md", "plan.md")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_NUMBER_RE = re.compile(r"(\d+)")


class TasksNotFoundError(Exception):
    """Raised when no ``tasks/`` folder can be located from the start path."""


@dataclass
class Task:
    """A single task, normalised from a file or a directory of fragments."""

    task_id: str
    title: str
    state: str
    path: Path
    body: str
    labels: list[str] = field(default_factory=list)
    priority: str | None = None
    # The owner's work-queue rank. Never written by an agent.
    next_rank: int | None = None
    # The frontmatter as written, for fields read but not interpreted here
    # (``claimed_by``, ``branch``, ``depends_on`` ...).
    meta: dict[str, object] = field(default_factory=dict)
    # The body of the task's own file: the whole body for a single file, the
    # ``description.md`` fragment for a directory task. The frontmatter and the
    # conversation live there, whatever else the directory holds.
    description: str = ""

    @property
    def number(self) -> str | None:
        """The leading id number of the filename (``052``), when it has one.

        Kept as text so the author's zero-padding survives into the display.
        """
        match = _NUMBER_RE.match(self.task_id)
        return match.group(1) if match else None

    @property
    def conversation(self) -> list[Entry]:
        """The thread at the bottom of the task's own file, oldest first."""
        return parse(self.description)

    @property
    def open_question(self) -> Entry | None:
        """The question nobody has answered yet, when there is one.

        Whoever did not ask is who the task is waiting on.
        """
        return open_question(self.conversation)

    @property
    def sort_key(self) -> tuple[int, str]:
        """Sort numerically by leading id number when present, else by name."""
        number = self.number
        return (int(number) if number else 1_000_000, self.task_id)


def find_tasks_dir(start: Path, folder_name: str = "tasks") -> Path:
    """Walk up from ``start`` to the nearest directory containing ``folder_name``.

    The returned path is the tasks folder itself. Raises
    :class:`TasksNotFoundError` if none is found up to the filesystem root.
    """
    start = start.resolve()
    candidates = [start, *start.parents] if start.is_dir() else list(start.parents)
    for directory in candidates:
        tasks = directory / folder_name
        if is_tasks_dir(tasks):
            return tasks
    raise TasksNotFoundError(
        f"No {folder_name}/ folder with open|ongoing|closed subfolders "
        f"found from {start}"
    )


def is_tasks_dir(path: Path) -> bool:
    """True if ``path`` is a tasks folder (has an open/ongoing/closed subdir)."""
    return _real_dir(path) and any(_real_dir(path / state) for state in STATES)


def _real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def count_states(tasks_dir: Path) -> dict[str, int]:
    """Cheap per-state entry counts (no frontmatter parsing), for summaries."""
    counts: dict[str, int] = {}
    for state in STATES:
        state_dir = tasks_dir / state
        counts[state] = (
            sum(1 for entry in state_dir.iterdir() if _is_task_entry(entry))
            if _real_dir(state_dir)
            else 0
        )
    return counts


def _is_task_entry(entry: Path) -> bool:
    if entry.is_symlink():
        return False
    return (entry.is_file() and entry.suffix == ".md") or entry.is_dir()


def load_tasks(tasks_dir: Path, states: tuple[str, ...] = STATES) -> list[Task]:
    """Load every task under the requested ``states``, sorted by id then state."""
    tasks: list[Task] = []
    for state in states:
        state_dir = tasks_dir / state
        if not _real_dir(state_dir):
            continue
        for entry in sorted(state_dir.iterdir()):
            try:
                task = _load_entry(entry, state)
            except FileNotFoundError:
                continue  # moved between listing and reading; it is someone else's now
            if task is not None:
                tasks.append(task)
    tasks.sort(
        key=lambda t: (
            t.next_rank is None,
            t.next_rank or 0,
            t.sort_key,
            STATES.index(t.state),
        )
    )
    return tasks


def _load_entry(entry: Path, state: str) -> Task | None:
    """Turn a file or directory into a :class:`Task`, or ``None`` if unusable."""
    if not _is_task_entry(entry):
        return None
    if entry.is_file() and entry.suffix == ".md":
        raw = _read(entry)
        meta, body = _split_frontmatter(raw)
        return _build_task(entry.stem, state, entry, body, meta)
    if entry.is_dir():
        return _load_dir_entry(entry, state)
    return None


def _load_dir_entry(entry: Path, state: str) -> Task | None:
    """Concatenate a directory task's markdown fragments into one body."""
    fragments = ordered_fragments(entry)
    if not fragments:
        return None
    meta: dict = {}
    description: str | None = None
    sections: list[str] = []
    for fragment in fragments:
        raw = _read(fragment)
        frag_meta, frag_body = _split_frontmatter(raw)
        # First fragment with frontmatter wins for title/labels/priority, and
        # is the task's own file: where the thread is read from and written to.
        if frag_meta and description is None:
            description = frag_body
        for key, value in frag_meta.items():
            meta.setdefault(key, value)
        sections.append(f"## _{fragment.stem}_\n\n{frag_body.strip()}")
    body = "\n\n---\n\n".join(sections)
    return _build_task(entry.name, state, entry, body, meta, description)


def ordered_fragments(entry: Path) -> list[Path]:
    """Known fragments first in a stable order, then any other ``*.md`` files."""
    known = [entry / name for name in _FRAGMENT_ORDER if _real_file(entry / name)]
    extras = sorted(
        f for f in entry.glob("*.md") if f.name not in _FRAGMENT_ORDER and _real_file(f)
    )
    return known + extras


def _real_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _build_task(
    task_id: str,
    state: str,
    path: Path,
    body: str,
    meta: dict,
    description: str | None = None,
) -> Task:
    labels = meta.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    priority = meta.get("priority")
    return Task(
        task_id=task_id,
        title=_derive_title(meta, body, task_id),
        state=state,
        path=path,
        body=body.strip(),
        labels=[str(label) for label in labels],
        priority=str(priority) if priority is not None else None,
        next_rank=_as_rank(meta.get("next")),
        meta=dict(meta),
        description=(body if description is None else description).strip(),
    )


def _as_rank(value: object) -> int | None:
    """A queue rank is a positive integer; anything else is not a rank."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _derive_title(meta: dict, body: str, fallback: str) -> str:
    """Title from frontmatter, else the first ``# heading``, else the id."""
    title = meta.get("title")
    if title:
        return str(title).strip()
    heading = _HEADING_RE.search(body)
    if heading:
        return heading.group(1).strip()
    return fallback


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    """Return ``(metadata, body)``; metadata is empty when parsing fails."""
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(meta, dict):
        return {}, raw
    return meta, match.group(2)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")
