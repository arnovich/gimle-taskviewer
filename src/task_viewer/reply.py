"""Answer a task's thread from tv, and put the entry where the agents look.

An agent waiting in ``grind`` reads the thread from ``origin/main``, so an
entry left in the working tree reaches nobody. The entry is appended in a
throwaway detached worktree of the remote's default branch, committed as that
one file and pushed — the same shape as an agent's claim. The owner's checkout
is never written: a task file edited locally blocks the next pull.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .conversation import Entry, append
from .git_info import load_git_info
from .remote import (
    FETCH_LOCK_RE,
    FETCH_TIMEOUT,
    PUSH_RACE_RE,
    CommandResult,
    fast_forward,
    run_git,
)
from .textfile import TextFileError

ATTEMPTS = 3
LOCK_RETRIES = 5
STATES = ("open", "ongoing", "closed")


@dataclass(frozen=True)
class PushResult:
    """Outcome of a push, worded for the notification it becomes."""

    ok: bool
    message: str
    branch: str = ""
    # On success: the task file's path on that branch and its content after
    # the entry, so the list can show the thread as main now has it.
    path: str = ""
    content: str = ""


class AmbiguousTask(LookupError):
    """More than one task carries the number; automation must not guess."""


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


def find_task_file(tree: Path, number: str, stem: str | None = None) -> Path | None:
    """The task's own file under ``tree/tasks``, whichever state folder holds it.

    A single-file task is ``NNN-*.md``; a directory task keeps its thread in
    ``NNN-*/description.md``. The local copy may be stale — an agent moves the
    file to ``ongoing/`` when it claims and to ``closed/`` when its PR opens —
    so the search is by number, not by the path the checkout knows. Two tasks
    with the same number are refused unless ``stem`` (the local file's name)
    picks one out: the standard says an ambiguous number is never acted on.
    """
    found: list[Path] = []
    for state in STATES:
        folder = tree / "tasks" / state
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.glob(f"{number}-*")):
            if candidate.is_file() and candidate.suffix == ".md":
                found.append(candidate)
            elif candidate.is_dir() and (candidate / "description.md").is_file():
                found.append(candidate / "description.md")
    if len(found) > 1 and stem:
        exact = [path for path in found if path.stem == stem or path.parent.name == stem]
        if len(exact) == 1:
            return exact[0]
    if len(found) > 1:
        names = ", ".join(str(path.relative_to(tree)) for path in found)
        raise AmbiguousTask(f"task {number} is ambiguous on that branch: {names}")
    return found[0] if found else None


def push_entry(
    root: Path, number: str, entry: Entry, *, stem: str | None = None, attempts: int = ATTEMPTS
) -> PushResult:
    """Append ``entry`` to the task's thread on origin's default branch.

    Works in a throwaway detached worktree of ``origin/<default>``, so the
    checkout at ``root`` can be dirty or on any branch. A push that loses the
    race is retried from the remote's new tip with the entry appended again;
    a fetch that loses a ref lock to tv's own refresh waits and retries.
    """
    branch = default_branch(root)
    error = ""
    for _ in range(attempts):
        fetched = _fetch(root, branch)
        if not fetched.ok:
            return PushResult(False, f"could not fetch origin/{branch}: {_reason(fetched)}")
        try:
            parent = Path(tempfile.mkdtemp(prefix=f"tv-reply-{number}-"))
        except OSError as failure:
            return PushResult(False, f"could not make a temporary worktree: {failure}")
        work = parent / "tree"
        try:
            added = run_git(
                root, "worktree", "add", "--quiet", "--detach", str(work),
                f"refs/remotes/origin/{branch}", timeout=FETCH_TIMEOUT,
            )
            if not added.ok:
                return PushResult(False, f"could not check out origin/{branch}: {_reason(added)}")
            try:
                target = find_task_file(work, number, stem)
            except AmbiguousTask as failure:
                return PushResult(False, f"{failure} — nothing pushed")
            if target is None:
                return PushResult(False, f"task {number} is not on origin/{branch} — nothing pushed")
            try:
                append(target, entry)
                content = target.read_text(encoding="utf-8")
            except (TextFileError, OSError, UnicodeDecodeError) as failure:
                return PushResult(False, f"could not write the entry: {failure}")
            rel = target.relative_to(work).as_posix()
            committed = _commit(work, rel, f"task {number}: {entry.kind}")
            if not committed.ok:
                return PushResult(False, f"commit failed: {_reason(committed)}")
            pushed = run_git(
                work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}",
                timeout=FETCH_TIMEOUT,
            )
            if pushed.ok:
                return PushResult(True, f"{entry.kind} pushed to origin/{branch}", branch, rel, content)
            error = _reason(pushed)
            if not PUSH_RACE_RE.search(pushed.error):
                return PushResult(False, f"push failed: {error}")
        finally:
            run_git(root, "worktree", "remove", "--force", str(work), timeout=FETCH_TIMEOUT)
            shutil.rmtree(parent, ignore_errors=True)
            run_git(root, "worktree", "prune", timeout=FETCH_TIMEOUT)
    return PushResult(False, f"push rejected {attempts} times in a row: {error}")


def _fetch(root: Path, branch: str) -> CommandResult:
    """Fetch the branch, waiting out a ref lock held by another fetch of this repo."""
    fetched = CommandResult(False, "", "not fetched")
    for attempt in range(1, LOCK_RETRIES + 1):
        fetched = run_git(root, "fetch", "--quiet", "origin", branch, timeout=FETCH_TIMEOUT)
        if fetched.ok or not FETCH_LOCK_RE.search(fetched.error) or attempt == LOCK_RETRIES:
            return fetched
        time.sleep(0.2 * attempt)
    return fetched


def _commit(work: Path, rel: str, message: str) -> CommandResult:
    """Stage and commit one path; a hook that rewrites the file gets one more try.

    The commit is then checked to carry that path and nothing else: a hook
    that stages other files must not ride to ``main`` on a reply.
    """
    committed = CommandResult(False, "", "nothing committed")
    for _ in range(2):
        staged = run_git(work, "add", "--", rel, timeout=FETCH_TIMEOUT)
        if not staged.ok:
            return staged
        committed = run_git(work, "commit", "--quiet", "-m", message, "--", rel, timeout=FETCH_TIMEOUT)
        if committed.ok:
            break
    if not committed.ok:
        return committed
    carried = run_git(work, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", timeout=FETCH_TIMEOUT)
    files = carried.out.split()
    if files != [rel]:
        return CommandResult(False, "", f"the commit carries {files}, not only {rel} (a hook staged more)")
    return committed


def refresh_checkout(root: Path, branch: str) -> str:
    """Fast-forward ``root`` onto what was just pushed, when that is safe.

    Only a clean checkout of the default branch that tracks ``origin/<branch>``
    is moved, through the same guarded path as the ``u`` key; anything else —
    a task branch, a fork's ``main``, local edits — is left alone and named,
    so the owner knows the file on disk is behind what the list shows.
    """
    info = load_git_info(root)
    if info is None:
        return ""
    if info.branch != branch or info.upstream != f"origin/{branch}":
        return f"checkout is on {info.branch}; the file here is behind origin/{branch}"
    result = fast_forward(root, refresh=False)
    return "" if result.ok else f"checkout not updated: {result.message}"


def _reason(result: CommandResult) -> str:
    """The first line git gave, for a notification that says why."""
    text = (result.error or result.out).strip()
    return text.splitlines()[0] if text else "git gave no reason"
