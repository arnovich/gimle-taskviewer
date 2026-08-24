"""Bring a checkout into step with its remote.

:mod:`git_info` only ever reads. These two operations reach the network and can
move the working tree, so they live apart: ``fetch`` refreshes what git knows
about the remote, ``fast_forward`` applies it. Both refuse to prompt — a
credential prompt inside a full-screen TUI would simply hang it.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .git_info import load_git_info

# A fetch is a network round trip; a wedged remote must not pin the worker.
FETCH_TIMEOUT = 30.0


@dataclass
class UpdateResult:
    """Outcome of an update attempt, worded for the notification it becomes."""

    ok: bool
    message: str


def fetch(root: Path, timeout: float = FETCH_TIMEOUT) -> bool:
    """Refresh remote-tracking refs for ``root``. False on any failure.

    Read-only: it moves ``refs/remotes/*`` and nothing in the working tree.
    """
    return _run(root, "fetch", "--quiet", timeout=timeout) is not None


def fast_forward(root: Path) -> UpdateResult:
    """Advance the checked-out branch to its upstream, or explain why not.

    ``--ff-only`` is the whole safety story: git either moves the branch
    pointer forward or refuses, so this can never produce a merge commit or
    leave conflicts behind. A dirty tree is refused before that even runs,
    because a fast-forward can still overwrite an uncommitted file.
    """
    info = load_git_info(root)
    if info is None:
        return UpdateResult(False, "not a git checkout")
    if info.upstream is None:
        reason = "no upstream branch to update from"
        if not info.has_remote:
            reason = "no remote configured"
        return UpdateResult(False, reason)
    if info.dirty:
        files = "file" if info.dirty == 1 else "files"
        return UpdateResult(False, f"{info.dirty} uncommitted {files} — commit or stash first")
    if not info.unpulled:
        return UpdateResult(True, f"already up to date with {info.upstream}")

    if _run(root, "merge", "--ff-only", info.upstream, timeout=FETCH_TIMEOUT) is None:
        return UpdateResult(False, f"cannot fast-forward — diverged from {info.upstream}")
    commits = "commit" if info.unpulled == 1 else "commits"
    return UpdateResult(True, f"fast-forwarded {info.unpulled} {commits}")


def _run(root: Path, *args: str, timeout: float) -> str | None:
    """Run a git command that may touch the network; ``None`` on any failure."""
    env = {
        **os.environ,
        # Fail rather than block on a credential or host-key prompt.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    }
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None
