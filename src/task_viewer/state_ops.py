"""Move a task between open/ongoing/closed and keep its frontmatter in sync.

State is primarily encoded by which subfolder a task lives in. When a task's
markdown carries a ``state:`` frontmatter field we update it too, so the file
and the folder never disagree. Frontmatter is edited with a targeted line
replacement rather than a YAML round-trip, to preserve the author's formatting.
"""

from __future__ import annotations

import re
from pathlib import Path

from .discovery import STATES, Task

_FRONTMATTER_BLOCK_RE = re.compile(r"\A(---[ \t]*\n)(.*?)(\n---[ \t]*\n)", re.DOTALL)
_STATE_LINE_RE = re.compile(r"^([ \t]*state:[ \t]*).*$", re.IGNORECASE | re.MULTILINE)


class StateChangeError(Exception):
    """Raised when a task cannot be moved to the requested state."""


def set_state(task: Task, new_state: str, tasks_dir: Path) -> Path:
    """Move ``task`` into ``tasks_dir/new_state`` and sync its frontmatter.

    Works for both single-file and directory-style tasks. Returns the new path.
    A no-op move (already in the target state) still refreshes the frontmatter.
    """
    if new_state not in STATES:
        raise StateChangeError(f"unknown state: {new_state!r}")

    dest_dir = tasks_dir / new_state
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / task.path.name

    if dest != task.path:
        if dest.exists():
            raise StateChangeError(f"a task already exists at {dest}")
        task.path.rename(dest)

    _sync_frontmatter(dest, new_state)
    return dest


def _sync_frontmatter(path: Path, new_state: str) -> None:
    if path.is_file():
        _rewrite_state(path, new_state)
        return
    for fragment in path.glob("*.md"):
        _rewrite_state(fragment, new_state)


def _rewrite_state(md_file: Path, new_state: str) -> None:
    """Update or insert the ``state:`` line inside a file's frontmatter block."""
    text = md_file.read_text(encoding="utf-8", errors="replace")
    block = _FRONTMATTER_BLOCK_RE.match(text)
    if block is None:
        return  # No frontmatter to keep in sync; the folder is the source of truth.

    open_fence, body, close_fence = block.group(1), block.group(2), block.group(3)
    if _STATE_LINE_RE.search(body):
        new_body = _STATE_LINE_RE.sub(rf"\g<1>{new_state}", body, count=1)
    else:
        new_body = f"{body}\nstate: {new_state}"

    updated = open_fence + new_body + close_fence + text[block.end():]
    if updated != text:
        md_file.write_text(updated, encoding="utf-8")
