"""Summarise the git state of a project checkout.

Answers three questions about a checkout: when it was created, when it was last
touched, and how far it has drifted from a base branch. Everything here is a
thin, read-only wrapper over the ``git`` CLI, returning plain data — the UI
decides how to word it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Base branches tried in order, as (display name, fully qualified ref). Fully
# qualified because git resolves a *tag* named `main` ahead of the branch, which
# would silently skew every count.
_BASE_CANDIDATES = (
    ("main", "refs/heads/main"),
    ("origin/main", "refs/remotes/origin/main"),
    ("master", "refs/heads/master"),
    ("origin/master", "refs/remotes/origin/master"),
)

# Reflog messages that mark a checkout genuinely coming into being. Branch
# reflogs are trimmed by `gc.reflogExpire`, so the oldest surviving entry is
# usually just an old pull — dating a repo from that is worse than saying
# nothing at all.
_CREATION_REFLOG = ("clone:", "branch: Created from", "commit (initial):")

# Commit subjects listed for a branch: enough to see the shape of the work.
_MAX_SUBJECTS = 5

# This feeds a summary line, never a decision, so a wedged repo must not hang
# the caller for long.
_TIMEOUT = 5.0

_REFLOG_STAMP_RE = re.compile(r"@\{(\d+)\}")


@dataclass
class GitInfo:
    """What a checkout looks like right now, relative to its base branch."""

    branch: str
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
        """The noun for this checkout: a linked worktree or a plain clone."""
        return "worktree" if self.is_worktree else "repository"

    @property
    def merged(self) -> bool:
        """True when the base branch already holds every commit here.

        For a worktree that means the branch is done and the directory can go;
        for a main checkout it just means nothing is waiting to be pushed.
        """
        return self.base is not None and self.ahead == 0


def load_git_info(root: Path) -> GitInfo | None:
    """Describe the checkout at ``root``, or ``None`` if it is not one."""
    if not (root / ".git").exists():
        return None
    branch = _branch(root)
    if branch is None:
        return None

    git_dir = _git(root, "rev-parse", "--absolute-git-dir") or ""
    # A linked worktree's git dir lives under `<repo>/.git/worktrees/<name>`. A
    # submodule also has a `.git` *file*, so the path — not the file type — is
    # what tells the two apart.
    is_worktree = "/worktrees/" in git_dir
    detached = branch == "HEAD"
    dirty_paths = _dirty_paths(root)
    base, base_ref = _pick_base(root, branch)
    behind, ahead = _drift(root, base_ref)
    return GitInfo(
        branch=_detached_name(root) if detached else branch,
        is_worktree=is_worktree,
        created=_created(root, None if detached else branch, is_worktree),
        updated=_updated(root, dirty_paths),
        base=base,
        repo=_repo_name(git_dir) if is_worktree else None,
        ahead=ahead,
        behind=behind,
        subjects=_subjects(root, base_ref) if base_ref and ahead else [],
        dirty=len(dirty_paths),
    )


def _created(root: Path, branch: str | None, is_worktree: bool) -> datetime | None:
    """When this checkout came into being, or ``None`` if that is unknowable.

    ``git worktree add`` writes the ``.git`` pointer file at creation, so its
    mtime dates a worktree (only ``git worktree move``/``repair`` rewrite it).
    A plain clone has no such marker, so we fall back to the reflog — but only
    when its oldest entry is genuinely a creation event.
    """
    if is_worktree:
        stamp = _mtime(root / ".git")
        if stamp is not None:
            return stamp
    return _branch_created(root, branch) if branch else None


def _updated(root: Path, dirty: list[Path]) -> datetime | None:
    """The most recent sign of life: a commit, or an uncommitted edit."""
    stamps = [_last_commit(root), *(_mtime(path) for path in dirty)]
    known = [stamp for stamp in stamps if stamp is not None]
    return max(known) if known else None


def _drift(root: Path, base_ref: str | None) -> tuple[int, int]:
    """``(behind, ahead)`` commit counts for HEAD against ``base_ref``."""
    if base_ref is None:
        return (0, 0)
    out = _git(root, "rev-list", "--left-right", "--count", f"{base_ref}...HEAD")
    parts = out.split() if out else []
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return (0, 0)
    return (int(parts[0]), int(parts[1]))


def _branch(root: Path) -> str | None:
    """The checked-out branch, the literal ``"HEAD"`` when detached, or ``None``.

    ``None`` means there is nothing to read at all — no commits yet, or not a
    working tree we can inspect.
    """
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")


def _detached_name(root: Path) -> str:
    """Display name for a detached HEAD — mid-rebase, mid-bisect, on a tag."""
    sha = _git(root, "rev-parse", "--short", "HEAD")
    return f"detached at {sha}" if sha else "detached"


def _pick_base(root: Path, branch: str) -> tuple[str | None, str | None]:
    """First base branch that exists and isn't the one we're standing on.

    Returns ``(display name, ref)``. On ``main`` itself the local branch says
    nothing, so the comparison falls through to ``origin/main`` and shows
    unpushed work instead.
    """
    out = _git(
        root, "for-each-ref", "--format=%(refname)", *(r for _, r in _BASE_CANDIDATES)
    )
    existing = set(out.splitlines()) if out else set()
    for name, ref in _BASE_CANDIDATES:
        if name != branch and ref in existing:
            return name, ref
    return None, None


def _repo_name(git_dir: str) -> str | None:
    """Directory name of the repository a linked worktree belongs to."""
    common = Path(git_dir.partition("/worktrees/")[0])
    name = common.parent.name if common.name == ".git" else common.stem
    return name or None


def _subjects(root: Path, base_ref: str) -> list[str]:
    """Subject lines of the commits on HEAD but not on ``base_ref``, newest first."""
    out = _git(
        root,
        "log",
        f"--max-count={_MAX_SUBJECTS}",
        "--format=%s%x00",
        f"{base_ref}..HEAD",
    )
    if not out:
        return []
    # NUL-terminated so a commit with an empty subject still occupies a slot.
    records = out.split("\0")[:-1]
    return [record.strip("\n") or "(no subject)" for record in records]


def _last_commit(root: Path) -> datetime | None:
    return _parse_iso(_git(root, "log", "-1", "--format=%cI"))


def _branch_created(root: Path, branch: str) -> datetime | None:
    """Date the branch's oldest reflog entry, if it records a creation.

    ``--date=unix`` turns the ``%gd`` selector into ``main@{1690000000}``; the
    last line is the oldest entry, and ``%gs`` says what that entry was.
    """
    out = _git(root, "reflog", "show", "--date=unix", "--format=%gd%x09%gs", branch)
    if not out:
        return None
    selector, _, message = out.splitlines()[-1].partition("\t")
    if not message.startswith(_CREATION_REFLOG):
        return None  # The reflog was trimmed; its tail is not a birth date.
    match = _REFLOG_STAMP_RE.search(selector)
    return _from_timestamp(int(match.group(1))) if match else None


def _dirty_paths(root: Path) -> list[Path]:
    """Paths git reports as changed or untracked (ignored files excluded).

    ``-z`` keeps paths verbatim: without it git C-quotes anything non-ASCII and
    the quoted name no longer names a file we can stat. ``-uall`` lists
    untracked files individually — the default collapses a whole tree into one
    ``dir/`` record, whose mtime ignores edits inside it. Each record is
    ``XY<space>path``; a rename adds a second record holding the original path,
    which we skip in favour of the live one.
    """
    out = _git(root, "status", "--porcelain", "-z", "-uall")
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
            # Paths are bytes, not necessarily UTF-8. surrogateescape round-trips
            # them back through os.fsencode() when we later stat the file.
            errors="surrogateescape",
            timeout=_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # Only the trailing newline is trimmed: `git status` records begin with a
    # significant space (" M path") that a full strip() would eat.
    return proc.stdout.rstrip("\n") or None


def _mtime(path: Path) -> datetime | None:
    try:
        return _from_timestamp(path.stat().st_mtime)
    except OSError:
        return None


def _from_timestamp(stamp: float) -> datetime | None:
    """Localise a POSIX timestamp, or ``None`` if it is not a real date.

    A corrupt reflog or a filesystem restored from a foreign archive can carry
    a timestamp outside the platform's range.
    """
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _minutes_since(moment: datetime | None, now: datetime | None) -> int | None:
    if moment is None:
        return None
    now = now or datetime.now(timezone.utc).astimezone()
    return int((now - moment).total_seconds() // 60)


def describe_age(moment: datetime | None, now: datetime | None = None) -> str:
    """A compact relative age for a narrow list row: ``3w``, ``5m``, ``now``."""
    minutes = _minutes_since(moment, now)
    if minutes is None:
        return "—"
    if minutes < 0:
        return "ahead"
    if minutes < 1:
        return "now"
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


def format_moment(moment: datetime | None, now: datetime | None = None) -> str:
    """Absolute local timestamp with a readable relative age, for the pane."""
    minutes = _minutes_since(moment, now)
    if moment is None or minutes is None:
        return "unknown"
    stamp = moment.strftime("%Y-%m-%d %H:%M")
    if minutes < 0:
        return f"{stamp} (in the future)"
    if minutes < 1:
        return f"{stamp} (just now)"
    return f"{stamp} ({describe_age(moment, now)} ago)"
