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
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .remote import FETCH_TIMEOUT, CommandResult, fetch, run_git

# Long enough that a page load never waits on the network twice in a row,
# short enough that an answer pushed from an editor shows up on the next look.
DEFAULT_MAX_AGE = 60.0

# git's own words for "someone pushed first" — the one push failure worth
# retrying. Anything else (auth, a missing branch) is reported as it is.
_REJECTED_RE = re.compile(r"rejected|fetch first|non-fast-forward", re.IGNORECASE)

_URL_TAIL_RE = re.compile(r"([^/:]+?)(?:\.git)?/?$")


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
    """``gimle-mimir`` from any of the ways a GitHub remote is spelled."""
    match = _URL_TAIL_RE.search(url.strip())
    if not match:
        raise MirrorError(f"cannot name a repository from {url!r}")
    return match.group(1)


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

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def last_refresh(self) -> RefreshResult:
        return RefreshResult(False, self._reached, self._reached_at)

    def ensure(self) -> None:
        """Clone the repository if it is not here yet, and learn its branch."""
        with self._lock:
            if not (self.root / ".git").exists():
                self.root.parent.mkdir(parents=True, exist_ok=True)
                result = run_git(
                    self.root.parent,
                    "clone",
                    "--quiet",
                    self.url,
                    self.root.name,
                    timeout=FETCH_TIMEOUT * 10,
                )
                if not result.ok:
                    raise MirrorError(f"{self.name}: clone failed: {_last_line(result)}")
                self._mark_reached()
            self.branch = self._current_branch()

    def refresh(self, max_age: float = DEFAULT_MAX_AGE, force: bool = False) -> RefreshResult:
        """Bring the checkout to the remote's tip, unless a recent fetch already did.

        A failed fetch still resets to the last known tip, so a checkout left
        mid-write by a crash heals on the next look.
        """
        with self._lock:
            self.ensure()
            now = time.monotonic()
            fresh = (
                self._last_attempt is not None and now - self._last_attempt < max_age
            )
            if fresh and not force:
                return RefreshResult(False, self._reached, self._reached_at)
            self._last_attempt = now
            reached = fetch(self.root)
            if reached:
                self._mark_reached()
            else:
                self._reached = False
            self._reset()
            detail = "" if reached else "could not reach the remote"
            return RefreshResult(True, reached, self._reached_at, detail)

    def commit_push(
        self,
        edit: Callable[[Path], Iterable[Path]],
        message: str,
        attempts: int = 3,
    ) -> bool:
        """Apply ``edit`` to a fresh checkout and push the result.

        ``edit`` is called with the checkout root and returns the files it
        changed; it must be safe to run again, because a push that loses the
        race is retried from the remote's new tip with the edit reapplied. An
        edit that returns nothing means there was nothing to do; ``False``.
        """
        with self._lock:
            error = ""
            for _ in range(attempts):
                self.refresh(force=True)
                try:
                    changed = [self._relative(path) for path in edit(self.root)]
                    if not changed:
                        return False
                    self._commit(changed, message)
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
                error = _last_line(pushed)
                self._reset()
                if not _REJECTED_RE.search(pushed.error):
                    raise MirrorError(f"{self.name}: push failed: {error}")
            raise MirrorError(
                f"{self.name}: push rejected {attempts} times in a row: {error}"
            )

    def _commit(self, paths: list[str], message: str) -> None:
        added = run_git(self.root, "add", "-A", "--", *paths, timeout=FETCH_TIMEOUT)
        if not added.ok:
            raise MirrorError(f"{self.name}: git add failed: {_last_line(added)}")
        committed = run_git(
            self.root, "commit", "--quiet", "-m", message, timeout=FETCH_TIMEOUT
        )
        if not committed.ok:
            raise MirrorError(f"{self.name}: commit failed: {_last_line(committed)}")

    def _reset(self) -> None:
        if self.branch is None:
            return
        run_git(
            self.root,
            "reset",
            "--quiet",
            "--hard",
            f"origin/{self.branch}",
            timeout=FETCH_TIMEOUT,
        )

    def _current_branch(self) -> str:
        result = run_git(self.root, "rev-parse", "--abbrev-ref", "HEAD", timeout=FETCH_TIMEOUT)
        if not result.ok or result.out.strip() in ("", "HEAD"):
            raise MirrorError(f"{self.name}: checkout is not on a branch")
        return result.out.strip()

    def _relative(self, path: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.root.resolve()))
        except ValueError as error:
            raise MirrorError(f"{self.name}: {path} is outside the mirror") from error

    def _mark_reached(self) -> None:
        self._reached = True
        self._reached_at = datetime.now(timezone.utc)


def _last_line(result: CommandResult) -> str:
    lines = [line.strip() for line in result.error.splitlines() if line.strip()]
    return lines[-1] if lines else "git gave no reason"
