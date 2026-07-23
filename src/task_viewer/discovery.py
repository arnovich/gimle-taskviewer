"""Locate a project's ``tasks/`` folder and load its markdown task files.

A gimle project keeps tasks under ``tasks/open`` and ``tasks/closed``. A task is
either a single ``*.md`` file or a directory holding several markdown fragments
(``description.md``, ``spec.md``, ``plan.md`` ...). Both shapes are normalised
into a :class:`Task`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# States map one-to-one onto the subdirectories of ``tasks/``.
STATES = ("open", "closed")

# Order to concatenate fragments of a directory-style task, most-relevant first.
_FRAGMENT_ORDER = ("description.md", "spec.md", "plan.md")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


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

    @property
    def sort_key(self) -> tuple[int, str]:
        """Sort numerically by leading id number when present, else by name."""
        match = re.match(r"(\d+)", self.task_id)
        number = int(match.group(1)) if match else 1_000_000
        return (number, self.task_id)


def find_tasks_dir(start: Path) -> Path:
    """Walk up from ``start`` to the nearest directory containing ``tasks/``.

    The returned path is the ``tasks/`` directory itself. Raises
    :class:`TasksNotFoundError` if none is found up to the filesystem root.
    """
    start = start.resolve()
    candidates = [start, *start.parents] if start.is_dir() else list(start.parents)
    for directory in candidates:
        tasks = directory / "tasks"
        if tasks.is_dir() and _looks_like_tasks_dir(tasks):
            return tasks
    raise TasksNotFoundError(
        f"No tasks/ folder with open|closed subfolders found from {start}"
    )


def load_tasks(tasks_dir: Path, states: tuple[str, ...] = STATES) -> list[Task]:
    """Load every task under the requested ``states``, sorted by id then state."""
    tasks: list[Task] = []
    for state in states:
        state_dir = tasks_dir / state
        if not state_dir.is_dir():
            continue
        for entry in sorted(state_dir.iterdir()):
            task = _load_entry(entry, state)
            if task is not None:
                tasks.append(task)
    tasks.sort(key=lambda t: (t.sort_key, STATES.index(t.state)))
    return tasks


def _looks_like_tasks_dir(tasks: Path) -> bool:
    return any((tasks / state).is_dir() for state in STATES)


def _load_entry(entry: Path, state: str) -> Task | None:
    """Turn a file or directory into a :class:`Task`, or ``None`` if unusable."""
    if entry.is_file() and entry.suffix == ".md":
        raw = _read(entry)
        meta, body = _split_frontmatter(raw)
        return _build_task(entry.stem, state, entry, body, meta)
    if entry.is_dir():
        return _load_dir_entry(entry, state)
    return None


def _load_dir_entry(entry: Path, state: str) -> Task | None:
    """Concatenate a directory task's markdown fragments into one body."""
    fragments = _ordered_fragments(entry)
    if not fragments:
        return None
    meta: dict = {}
    sections: list[str] = []
    for fragment in fragments:
        raw = _read(fragment)
        frag_meta, frag_body = _split_frontmatter(raw)
        # First fragment with frontmatter wins for title/labels/priority.
        for key, value in frag_meta.items():
            meta.setdefault(key, value)
        sections.append(f"## _{fragment.stem}_\n\n{frag_body.strip()}")
    return _build_task(entry.name, state, entry, "\n\n---\n\n".join(sections), meta)


def _ordered_fragments(entry: Path) -> list[Path]:
    """Known fragments first in a stable order, then any other ``*.md`` files."""
    known = [entry / name for name in _FRAGMENT_ORDER if (entry / name).is_file()]
    extras = sorted(
        f for f in entry.glob("*.md") if f.name not in _FRAGMENT_ORDER
    )
    return known + extras


def _build_task(
    task_id: str, state: str, path: Path, body: str, meta: dict
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
    )


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
