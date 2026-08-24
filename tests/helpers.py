"""Helpers for building throwaway git repositories in tests.

The git modules are thin wrappers over the ``git`` CLI, so faking the CLI would
only test the fake — the tests drive real repositories instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> None:
    """Run a git command in ``cwd``, raising with its output on failure."""
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def commit(cwd: Path, name: str, message: str) -> None:
    """Write a one-line file and commit it."""
    (cwd / name).write_text(f"{name}\n", encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-m", message)


def init_repo(root: Path, branch: str = "main") -> Path:
    """A repository on ``branch`` with one commit."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", branch)
    commit(root, "README.md", "Initial commit")
    return root


def clone_repo(parent: Path, origin: Path, dest: Path) -> Path:
    """Clone ``origin`` to ``dest``; the remote is a local path, never a URL."""
    git(parent, "clone", "--quiet", str(origin), str(dest))
    return dest


def track_upstream(repo: Path, branch: str, upstream: str) -> None:
    """Point ``branch`` at ``upstream`` without contacting anything."""
    git(repo, "branch", f"--set-upstream-to={upstream}", branch)
