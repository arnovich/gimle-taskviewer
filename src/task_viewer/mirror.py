"""App-owned checkouts of the repositories the control plane watches.

The control plane never touches the owner's working directories. Each repo it
watches is cloned once into the app's own data directory and kept on the
remote's default branch — always clean, always at the remote's tip as of the
last fetch. That keeps every operation simple: a refresh is *fetch, then reset
to the remote tip*; a write is *edit, commit, push*, retried from the fresh
tip if the push is rejected. There is never a merge, because there is nothing
local to merge: the checkout is a cache of the remote plus, briefly, one
commit on its way out.

Fetching is lazy. A page load asks for a refresh and gets one only if the last
fetch is older than ``max_age``, so browsing does not hammer the remote; the
Refresh button forces one.

One caveat is inherent to the design: a push that times out *after* the
server accepted it is reported as a failure, and retrying it appends the same
entry twice. Read the thread before retrying a write that timed out.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .remote import FETCH_TIMEOUT, CommandResult, fetch, run_git

# Long enough that a page load never waits on the network twice in a row,
# short enough that an answer pushed from an editor shows up on the next look.
DEFAULT_MAX_AGE = 60.0

# git's own words for "someone pushed first" — the one push failure worth
# retrying. A hook or a protected branch also says "rejected", but as
# "[remote rejected]" and with its own reason, and retrying that is useless.
_RACE_RE = re.compile(r"\[rejected\][^\n]*\((?:fetch first|non-fast-forward|stale info)\)")

_URL_TAIL_RE = re.compile(r"([^/:]+?)(?:\.git)?/?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class MirrorError(Exception):
    """Raised when a mirror cannot be created or written through."""


@dataclass(frozen=True)
class RefreshResult:
    """What a refresh did, worded so the UI can say when it last looked."""

    fetched: bool  # a fetch was attempted on this call
    reached: bool  # the last attempted fetch got an answer
    at: datetime | None  # when the remote was last reached
    detail: str = ""


def name_from_url(url: str) -> str:
    """``gimle-mimir`` from any of the ways a GitHub remote is spelled.

    The name becomes a directory under the data dir, so it is held to a
    strict charset — no ``..``, no separators, nothing git would read as an
    option.
    """
    match = _URL_TAIL_RE.search(url.strip())
    name = match.group(1) if match else ""
    if not _NAME_RE.match(name) or set(name) <= {"."}:
        raise MirrorError(f"cannot name a repository from {url!r}")
    return name


class Mirror:
    """One repository, cloned for the control plane's own use."""

    def __init__(self, url: str, root: Path, name: str | None = None) -> None:
        self.url = url
        self.root = root
        self.name = name or name_from_url(url)
        self.branch: str | None = None
        self._lock = threading.RLock()
        self._last_attempt: float | None = None  # monotonic
        self._reached_at: datetime | None = None
        self._reached = False
        self._detail = ""

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def last_refresh(self) -> RefreshResult:
        return RefreshResult(False, self._reached, self._reached_at, self._detail)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the checkout still while reading it.

        A refresh or a write rewrites the tree; a reader that walks it in the
        middle sees files vanish. Readers take this, briefly.
        """
        with self._lock:
            yield

    def ensure(self) -> None:
        """Clone the repository if it is not here yet, and learn its branch."""
        with self._lock:
            if not (self.root / ".git").exists():
                self.root.parent.mkdir(parents=True, exist_ok=True)
                result = run_git(
                    self.root.parent,
                    "clone",
                    "--quiet",
                    # A symlink in the repo would otherwise be checked out as
                    # one and point the viewer at the owner's own files.
                    "-c",
                    "core.symlinks=false",
                    "--",
                    self.url,
                    self.root.name,
                    timeout=FETCH_TIMEOUT * 10,
                )
                if not result.ok:
                    raise MirrorError(f"{self.name}: clone failed: {_reason(result)}")
                self._mark_reached()
            if self.branch is None:
                run_git(self.root, "config", "core.symlinks", "false", timeout=FETCH_TIMEOUT)
                self.branch = self._current_branch()

    def refresh(self, max_age: float = DEFAULT_MAX_AGE, force: bool = False) -> RefreshResult:
        """Bring the checkout to the remote's tip, unless a recent fetch already did.

        A failed fetch still resets to the last known tip, so a checkout left
        mid-write by a crash heals on the next look. A reset that fails is
        reported in ``detail`` and blocks writes until it succeeds.
        """
        with self._lock:
            self.ensure()
            now = time.monotonic()
            fresh = (
                self._last_attempt is not None and now - self._last_attempt < max_age
            )
            if fresh and not force:
                return self.last_refresh
            self._last_attempt = now
            reached = fetch(self.root)
            if reached:
                self._mark_reached()
            else:
                self._reached = False
            problems = [] if reached else ["could not reach the remote"]
            reset = self._reset()
            if not reset.ok:
                problems.append(f"checkout could not be reset: {_reason(reset)}")
            self._detail = " · ".join(problems)
            return RefreshResult(True, reached, self._reached_at, self._detail)

    def commit_push(
        self,
        edit: Callable[[Path], Iterable[Path]],
        message: str,
        attempts: int = 3,
    ) -> bool:
        """Apply ``edit`` to a fresh checkout and push the result.

        ``edit`` is called with the checkout root and returns the files it
        changed; it must be safe to run again, because a push that loses the
        race is retried from the remote's new tip with the edit reapplied.
        Whether anything changed is decided from the tree, not from what the
        callback claims: an edit that left the tree as it was pushes nothing
        and returns ``False``.
        """
        with self._lock:
            error = ""
            for _ in range(attempts):
                refreshed = self.refresh(force=True)
                if "could not be reset" in refreshed.detail:
                    raise MirrorError(f"{self.name}: {refreshed.detail}")
                try:
                    paths = [self._relative(path) for path in edit(self.root)]
                    if not paths or not self._dirty():
                        self._reset()
                        return False
                    self._commit(paths, message)
                    pushed = run_git(
                        self.root,
                        "push",
                        "--quiet",
                        "origin",
                        f"HEAD:refs/heads/{self.branch}",
                        timeout=FETCH_TIMEOUT,
                    )
                except BaseException:
                    self._reset()
                    raise
                if pushed.ok:
                    self._mark_reached()
                    return True
                error = _reason(pushed)
                self._reset()
                if not _RACE_RE.search(pushed.error):
                    raise MirrorError(f"{self.name}: push failed: {error}")
            raise MirrorError(
                f"{self.name}: push rejected {attempts} times in a row: {error}"
            )

    def _dirty(self) -> bool:
        status = run_git(self.root, "status", "--porcelain", timeout=FETCH_TIMEOUT)
        return bool(status.out.strip())

    def _commit(self, paths: list[str], message: str) -> None:
        added = run_git(self.root, "add", "-A", "--", *paths, timeout=FETCH_TIMEOUT)
        if not added.ok:
            raise MirrorError(f"{self.name}: git add failed: {_reason(added)}")
        staged = run_git(self.root, "diff", "--cached", "--quiet", timeout=FETCH_TIMEOUT)
        if staged.ok:
            raise MirrorError(
                f"{self.name}: the edit changed files outside the ones it named"
            )
        committed = run_git(
            self.root, "commit", "--quiet", "-m", message, timeout=FETCH_TIMEOUT
        )
        if not committed.ok:
            raise MirrorError(f"{self.name}: commit failed: {_reason(committed)}")

    def _reset(self) -> CommandResult:
        if self.branch is None:
            return CommandResult(False, "", "branch unknown")
        return run_git(
            self.root,
            "reset",
            "--quiet",
            "--hard",
            f"refs/remotes/origin/{self.branch}",
            timeout=FETCH_TIMEOUT,
        )

    def _current_branch(self) -> str:
        # symbolic-ref, not rev-parse: it names the branch even before the
        # first commit exists, so an empty repository is reported as empty
        # rather than as "not on a branch".
        result = run_git(self.root, "symbolic-ref", "--short", "HEAD", timeout=FETCH_TIMEOUT)
        if not result.ok or not result.out.strip():
            raise MirrorError(f"{self.name}: checkout is not on a branch")
        return result.out.strip()

    def _relative(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root.resolve()))
        except ValueError as error:
            raise MirrorError(
                f"{self.name}: {Path(path).name} is outside the mirror"
            ) from error

    def _mark_reached(self) -> None:
        self._reached = True
        self._reached_at = datetime.now(timezone.utc)


def _reason(result: CommandResult) -> str:
    """The line of git's output that says why, not the trailer that says it failed."""
    lines = [line.strip() for line in result.error.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("!") or line.startswith("remote:"):
            return line
    if lines:
        return lines[-1]
    out = [line.strip() for line in result.out.splitlines() if line.strip()]
    return out[-1] if out else "git gave no reason"
