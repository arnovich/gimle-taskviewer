"""Bring a checkout into step with its remote.

:mod:`git_info` only ever reads. These operations reach the network and can
move the working tree, so they live apart: :func:`fetch` refreshes what git
knows about the remote, :func:`fast_forward` applies it.

Both run non-interactively. A credential or host-key prompt inside a
full-screen TUI would hang it, and an askpass helper would throw a modal dialog
over it — so every prompting mechanism is disabled and git is made to fail
instead.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .git_info import describe_age_phrase, load_git_info

# A fetch is a network round trip; a wedged remote must not pin the worker.
FETCH_TIMEOUT = 30.0

# Live child processes, so a quitting app does not wait on the network. Threads
# in a pool are not daemons and `subprocess.run` cannot be interrupted, so
# without this `q` can leave the user staring at a dead terminal.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()


@dataclass
class CommandResult:
    """What git actually said — the reason a refusal can be worded honestly."""

    ok: bool
    out: str
    error: str


@dataclass
class UpdateResult:
    """Outcome of an update attempt, worded for the notification it becomes."""

    ok: bool
    message: str


def cancel_all() -> None:
    """Kill any in-flight git child. Called when the app is shutting down."""
    with _running_lock:
        children = list(_running)
    for child in children:
        try:
            child.kill()
        except OSError:
            pass


def fetch(root: Path, timeout: float = FETCH_TIMEOUT) -> bool:
    """Refresh remote-tracking refs for ``root``. False if the remote was not reached.

    Read-only as far as the working tree is concerned: it moves
    ``refs/remotes/*`` and nothing else. ``--prune`` matters — without it a
    branch deleted on the server keeps a stale tracking ref forever and never
    reads as gone.
    """
    return _run(root, "fetch", "--quiet", "--prune", "--no-auto-gc", timeout=timeout).ok


def fast_forward(root: Path, refresh: bool = True) -> UpdateResult:
    """Advance the checked-out branch to its upstream, or explain why not.

    Fetches first by default: "already up to date" is worthless if it is
    measured against refs from an hour ago.

    ``--ff-only`` means git either moves the branch pointer forward or refuses,
    so this can never produce a merge commit or leave conflicts behind. That is
    not the whole safety story, though: a fast-forward will happily overwrite a
    file git has been told to ignore, so those are checked for separately.
    """
    if refresh:
        fetch(root)
    info = load_git_info(root)
    if info is None:
        return UpdateResult(False, "not a git checkout")
    if not info.has_remote:
        return UpdateResult(False, "no remote configured")
    if info.upstream is None:
        return UpdateResult(False, "no upstream branch to update from")
    if info.upstream_gone:
        return UpdateResult(False, f"{info.upstream} no longer exists on the remote")
    if not info.tracks_own_branch:
        # `git worktree add -b` inherits the parent's upstream, so a task branch
        # commonly tracks origin/main. Fast-forwarding onto that would move the
        # branch to main's tip and quietly discard what it was branched for.
        return UpdateResult(
            False,
            f"tracks `{info.upstream}`, not a branch of its own — "
            "updating would move it onto that branch",
        )
    if info.dirty:
        files = "file" if info.dirty == 1 else "files"
        return UpdateResult(
            False, f"{info.dirty} uncommitted {files} — commit or stash first"
        )
    if not info.unpulled:
        checked = describe_age_phrase(info.fetched)
        return UpdateResult(True, f"up to date with {info.upstream} as of {checked}")

    clashes = _ignored_clashes(root, info.upstream)
    if clashes:
        return UpdateResult(
            False,
            f"would overwrite ignored {_name_list(clashes)} — "
            "move them aside first",
        )

    result = _run(root, "merge", "--ff-only", info.upstream, timeout=FETCH_TIMEOUT)
    if not result.ok:
        return UpdateResult(False, _first_line(result.error) or "git refused the merge")
    commits = "commit" if info.unpulled == 1 else "commits"
    return UpdateResult(True, f"fast-forwarded {info.unpulled} {commits}")


def _ignored_clashes(root: Path, upstream: str) -> list[str]:
    """Incoming files that exist here only as ignored files.

    git refuses to clobber an *untracked* file, but silently overwrites an
    *ignored* one. A local ``.env`` is exactly the sort of thing that is
    ignored, exists nowhere else, and would be destroyed without a word.
    """
    incoming = _run(root, "diff", "--name-only", "-z", f"HEAD..{upstream}", timeout=FETCH_TIMEOUT)
    if not incoming.ok:
        return []
    candidates = [name for name in incoming.out.split("\0") if name and (root / name).exists()]
    if not candidates:
        return []
    # check-ignore reads paths on stdin, so any number of them is fine, and it
    # exits non-zero simply to mean "none of these are ignored".
    ignored = _run(
        root,
        "check-ignore",
        "--stdin",
        "-z",
        timeout=FETCH_TIMEOUT,
        stdin_text="\0".join(candidates),
    )
    return [name for name in ignored.out.split("\0") if name]


def _name_list(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(names[:limit])
    extra = len(names) - limit
    return f"{shown} and {extra} more" if extra > 0 else shown


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            # git prefixes its own diagnostics; the rest of the line is the news.
            for prefix in ("error: ", "fatal: ", "hint: "):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):]
                    break
            return stripped
    return ""


def _env() -> dict[str, str]:
    """An environment in which git cannot ask a human anything."""
    ssh = _config("core.sshCommand") or "ssh"
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        # GIT_TERMINAL_PROMPT alone does not stop an askpass helper, which on a
        # desktop pops a modal dialog per repository.
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
        # BatchMode already declines host-key confirmation, so no key is ever
        # accepted on our say-so. The user's own core.sshCommand is preserved:
        # it may carry the deploy key this remote actually needs.
        "GIT_SSH_COMMAND": f"{ssh} -oBatchMode=yes",
        "LC_ALL": "C",
    }


def _config(key: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _run(
    root: Path, *args: str, timeout: float, stdin_text: str | None = None
) -> CommandResult:
    """Run a git command that may touch the network or run hooks."""
    command = ["git", "-C", str(root), *args]
    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="surrogateescape",
            env=_env(),
        )
    except (OSError, ValueError) as error:
        return CommandResult(False, "", str(error))

    with _running_lock:
        _running.add(child)
    try:
        out, error = child.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.communicate()  # reap it; the pipes are already ours
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return CommandResult(False, "", f"timed out after {timeout:.0f}s")
    except (OSError, ValueError) as error:  # noqa: F841 - reported below
        child.kill()
        return CommandResult(False, "", "could not run git")
    finally:
        with _running_lock:
            _running.discard(child)
    return CommandResult(child.returncode == 0, out or "", error or "")
