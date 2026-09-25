"""Answer a task's thread from tv, and put the entry where the agents look.

An agent waiting in ``grind`` reads the thread from ``origin/main``, so an
entry left in the working tree reaches nobody. The entry is appended in a
throwaway detached worktree of the remote's default branch, committed as that
one file and pushed — the same shape as an agent's claim. The owner's checkout
is never written: a task file edited locally blocks the next pull.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .conversation import Entry, append
from .git_info import load_git_info
from .remote import FETCH_TIMEOUT, CommandResult, fast_forward, run_git

ATTEMPTS = 3
STATES = ("open", "ongoing", "closed")
_HANDLE_RE = re.compile(r"[^A-Za-z0-9._/@-]+")
_RACE_RE = re.compile(r"non-fast-forward|fetch first|stale info|rejected", re.IGNORECASE)


@dataclass(frozen=True)
class PushResult:
    """Outcome of a push, worded for the notification it becomes."""

    ok: bool
    message: str
    branch: str = ""


def owner_handle(configured: str = "") -> str:
    """The handle written on the owner's entries: one token, no ``/``.

    Configured wins; otherwise git's ``user.name`` squeezed to one token,
    as the control plane does; otherwise ``owner``. An agent's handle has a
    ``/`` in it, so one is never written here.
    """
    if configured.strip():
        return _handle(configured) or "owner"
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return "owner"
    return (_handle(proc.stdout) if proc.returncode == 0 else "") or "owner"


def _handle(text: str) -> str:
    return _HANDLE_RE.sub("", text).replace("/", "").strip("._-")


def default_branch(root: Path) -> str:
    """The remote's default branch: ``origin/HEAD`` if the clone recorded it."""
    head = run_git(
        root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
        timeout=FETCH_TIMEOUT,
    )
    name = head.out.strip()
    if head.ok and name.startswith("origin/"):
        return name[len("origin/"):]
    for candidate in ("main", "master"):
        probe = run_git(
            root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{candidate}",
            timeout=FETCH_TIMEOUT,
        )
        if probe.ok:
            return candidate
    return "main"


def find_task_file(tree: Path, task_id: str) -> Path | None:
    """The task's own file under ``tree/tasks``, whichever state folder holds it.

    A single-file task is ``NNN-*.md``; a directory task keeps its thread in
    ``NNN-*/description.md``. The local copy may be stale — an agent moves the
    file to ``ongoing/`` when it claims and to ``closed/`` when its PR opens —
    so the search is by number, not by the path the checkout knows.
    """
    for state in STATES:
        folder = tree / "tasks" / state
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.glob(f"{task_id}-*")):
            if candidate.is_file() and candidate.suffix == ".md":
                return candidate
            if candidate.is_dir() and (candidate / "description.md").is_file():
                return candidate / "description.md"
    return None


def push_entry(
    root: Path, task_id: str, entry: Entry, *, attempts: int = ATTEMPTS
) -> PushResult:
    """Append ``entry`` to the task's thread on origin's default branch.

    Works in a throwaway detached worktree of ``origin/<default>``, so the
    checkout at ``root`` can be dirty or on any branch. A push that loses the
    race is retried from the remote's new tip with the entry appended again.
    """
    branch = default_branch(root)
    error = ""
    for _ in range(attempts):
        fetched = run_git(root, "fetch", "--quiet", "origin", branch, timeout=FETCH_TIMEOUT)
        if not fetched.ok:
            return PushResult(False, f"could not fetch origin/{branch}: {_reason(fetched)}")
        parent = Path(tempfile.mkdtemp(prefix=f"tv-reply-{task_id}-"))
        work = parent / "tree"
        try:
            added = run_git(
                root, "worktree", "add", "--quiet", "--detach", str(work),
                f"refs/remotes/origin/{branch}", timeout=FETCH_TIMEOUT,
            )
            if not added.ok:
                return PushResult(False, f"could not check out origin/{branch}: {_reason(added)}")
            target = find_task_file(work, task_id)
            if target is None:
                return PushResult(False, f"task {task_id} is not on origin/{branch} — nothing pushed")
            append(target, entry)
            rel = target.relative_to(work).as_posix()
            committed = _commit(work, rel, f"task {task_id}: {entry.kind}")
            if not committed.ok:
                return PushResult(False, f"commit failed: {_reason(committed)}")
            pushed = run_git(
                work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}",
                timeout=FETCH_TIMEOUT,
            )
            if pushed.ok:
                return PushResult(True, f"{entry.kind} pushed to origin/{branch}", branch)
            error = _reason(pushed)
            if not _RACE_RE.search(pushed.error):
                return PushResult(False, f"push failed: {error}")
        finally:
            run_git(root, "worktree", "remove", "--force", str(work), timeout=FETCH_TIMEOUT)
            shutil.rmtree(parent, ignore_errors=True)
            run_git(root, "worktree", "prune", timeout=FETCH_TIMEOUT)
    return PushResult(False, f"push rejected {attempts} times in a row: {error}")


def _commit(work: Path, rel: str, message: str) -> CommandResult:
    """Stage and commit one path; a hook that rewrites the file gets one more try."""
    for _ in range(2):
        staged = run_git(work, "add", "--", rel, timeout=FETCH_TIMEOUT)
        if not staged.ok:
            return staged
        committed = run_git(work, "commit", "--quiet", "-m", message, "--", rel, timeout=FETCH_TIMEOUT)
        if committed.ok:
            return committed
    return committed


def refresh_checkout(root: Path, branch: str) -> str:
    """Fast-forward ``root`` onto what was just pushed, when that is safe.

    Only a clean checkout of the default branch itself is moved, through the
    same guarded path as the ``u`` key; anything else — a feature branch, a
    worktree, local edits — is left alone and named, so the owner knows the
    list still shows the file as it was.
    """
    info = load_git_info(root)
    if info is None:
        return ""
    if info.branch != branch:
        return f"checkout is on {info.branch}; pull to see it here"
    result = fast_forward(root, refresh=False)
    return "" if result.ok else f"checkout not updated: {result.message}"


def _reason(result: CommandResult) -> str:
    text = (result.error or result.out).strip()
    return text.splitlines()[0] if text else "git gave no reason"
