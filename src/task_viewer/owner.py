"""The handle written on the owner's entries, the same in tv and the control plane.

An author is one token; an agent's handle contains a ``/`` and an owner's never
does, and every tool tells the two apart by that alone.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HANDLE_RE = re.compile(r"[^A-Za-z0-9._@-]+")


def resolve_owner(configured: str, root: Path | None = None) -> str:
    """The handle written on the owner's entries.

    Configured wins; otherwise git's ``user.name`` (of ``root`` when given, so
    a per-repository name counts) with unsafe runs squeezed to ``-``;
    otherwise ``owner``.
    """
    if configured.strip():
        return handle(configured) or "owner"
    command = ["git", *(["-C", str(root)] if root else []), "config", "--get", "user.name"]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return "owner"
    return (handle(proc.stdout) if proc.returncode == 0 else "") or "owner"


def handle(text: str) -> str:
    """One token of safe characters with no ``/``, or ``""`` when nothing survives."""
    return _HANDLE_RE.sub("-", text.strip()).strip("-")
