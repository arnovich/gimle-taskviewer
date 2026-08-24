"""Summarise the git state of a project checkout.

A workspace like ``~/gimle`` is mostly *worktrees*: one repository checked out
many times, one branch per line of work. Browsing them, the questions are
always the same — when was this worktree made, has anything happened in it
lately, and how far has it drifted from ``main``? :func:`load_git_info` answers
those three with a handful of cheap git calls.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Branches tried, in order, as the baseline a checkout is compared against.
_BASE_CANDIDATES = ("main", "master", "origin/main", "origin/master")

# Commit subjects listed for a branch: enough to see the shape of the work.
MAX_SUBJECTS = 10

# Only so many dirty paths are stat'ed when dating uncommitted work.
_MAX_DIRTY_STATTED = 200

# This only feeds a summary line, so a slow repo must never stall the UI.
_TIMEOUT = 5.0

_REFLOG_STAMP_RE = re.compile(r"@\{(\d+)\}")


@dataclass
class GitInfo:
    """What a checkout looks like right now, relative to its base branch."""

    branch: str | None
    is_worktree: bool
    created: datetime | None
    updated: datetime | None
    base: str | None
    repo: str | None = None
    ahead: int = 0
    behind: int = 0
    subjects: list[str] = field(default_factory=list)
    dirty: int = 0

    @property
    def kind(self) -> str:
        return "worktree" if self.is_worktree else "repository"


def load_git_info(root: Path) -> GitInfo | None:
    """Describe the checkout at ``root``, or ``None`` if it is not one."""
    if not (root / ".git").exists():
        return None
    branch = _branch(root)
    if branch is None:
        return None

    is_worktree = (root / ".git").is_file()
    dirty = _dirty_paths(root)
    base = _pick_base(root, branch)
    behind, ahead = _drift(root, base)
    return GitInfo(
        branch=branch,
        is_worktree=is_worktree,
        created=_created(root, branch, is_worktree),
        updated=_updated(root, dirty),
        base=base,
        repo=_repo_name(root) if is_worktree else None,
        ahead=ahead,
        behind=behind,
        subjects=_subjects(root, base) if base and ahead else [],
        dirty=len(dirty),
    )


# --- the three questions ---------------------------------------------------


def _created(root: Path, branch: str, is_worktree: bool) -> datetime | None:
    """When this checkout came into being.

    ``git worktree add`` writes the ``.git`` pointer file once and never touches
    it again, so its mtime dates the worktree exactly. A plain clone has no such
    marker, so we fall back to the oldest surviving reflog entry for the current
    branch — the ``clone:``/``branch: Created from`` line.
    """
    if is_worktree:
        stamp = _mtime(root / ".git")
        if stamp is not None:
            return stamp
    return _branch_created(root, branch)


def _updated(root: Path, dirty: list[Path]) -> datetime | None:
    """The most recent sign of life: a commit, or an uncommitted edit."""
    stamps = [_last_commit(root), *(_mtime(p) for p in dirty[:_MAX_DIRTY_STATTED])]
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def _drift(root: Path, base: str | None) -> tuple[int, int]:
    """``(behind, ahead)`` commit counts for HEAD against ``base``."""
    if base is None:
        return (0, 0)
    out = _git(root, "rev-list", "--left-right", "--count", f"{base}...HEAD")
    if out is None:
        return (0, 0)
    parts = out.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return (0, 0)
    return (int(parts[0]), int(parts[1]))


# --- git plumbing ----------------------------------------------------------


def _branch(root: Path) -> str | None:
    """The checked-out branch, or ``None`` when HEAD is detached/unreadable."""
    name = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name in (None, "HEAD") else name


def _pick_base(root: Path, branch: str) -> str | None:
    """First existing base branch that isn't the one we're standing on.

    On ``main`` itself the local branch says nothing, so the comparison falls
    through to ``origin/main`` and shows unpushed/unpulled drift instead.
    """
    for name in _BASE_CANDIDATES:
        if name == branch:
            continue
        if _git(root, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"):
            return name
    return None


def _repo_name(root: Path) -> str | None:
    """Directory name of the repository a linked worktree belongs to."""
    common = _git(root, "rev-parse", "--git-common-dir")
    return Path(common).parent.name if common else None


def _subjects(root: Path, base: str) -> list[str]:
    """Subject lines of the commits on HEAD but not on ``base``, newest first."""
    out = _git(
        root, "log", f"--max-count={MAX_SUBJECTS}", "--format=%s", f"{base}..HEAD"
    )
    return out.splitlines() if out else []


def _last_commit(root: Path) -> datetime | None:
    return _parse_iso(_git(root, "log", "-1", "--format=%cI"))


def _branch_created(root: Path, branch: str) -> datetime | None:
    out = _git(root, "reflog", "show", "--date=unix", "--format=%gd", branch)
    if not out:
        return None
    match = _REFLOG_STAMP_RE.search(out.splitlines()[-1])
    return _from_timestamp(int(match.group(1))) if match else None


def _dirty_paths(root: Path) -> list[Path]:
    """Paths git reports as changed or untracked (ignored files excluded).

    ``-z`` keeps paths verbatim: without it git C-quotes anything non-ASCII, and
    the quoted name no longer names a file we can stat. Each record is
    ``XY<space>path``; a rename adds a second record holding the original path,
    which we skip in favour of the live one.
    """
    out = _git(root, "status", "--porcelain", "-z")
    if not out:
        return []
    paths: list[Path] = []
    skip_next = False
    for record in (r for r in out.split("\0") if r):
        if skip_next:
            skip_next = False
            continue
        status, name = record[:2], record[3:]
        skip_next = "R" in status or "C" in status
        if name:
            paths.append(root / name)
    return paths


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command in ``root``; ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # Only the trailing newline is trimmed: `git status` records begin with a
    # significant space (" M path") that a full strip() would eat.
    return proc.stdout.rstrip("\n") or None


# --- time helpers ----------------------------------------------------------


def _mtime(path: Path) -> datetime | None:
    try:
        return _from_timestamp(path.stat().st_mtime)
    except OSError:
        return None


def _from_timestamp(stamp: float) -> datetime:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone()


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def describe_age(moment: datetime | None, now: datetime | None = None) -> str:
    """A compact relative age such as ``3d`` or ``just now``."""
    if moment is None:
        return "—"
    now = now or datetime.now(timezone.utc).astimezone()
    delta = now - moment
    if delta < timedelta(0):
        return "just now"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 14:
        return f"{days}d"
    if days < 90:
        return f"{days // 7}w"
    return f"{days // 30}mo"


def format_moment(moment: datetime | None) -> str:
    """Absolute local timestamp plus its relative age."""
    if moment is None:
        return "unknown"
    return f"{moment.strftime('%Y-%m-%d %H:%M')} ({describe_age(moment)} ago)"
