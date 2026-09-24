"""Read and replace a task file without losing anyone else's edit.

Task files have several writers — ``tv``, the groom pass, the `grind` agent,
the control plane — so every edit here is *read exactly, change, replace only
if unchanged*. Writing in place would also truncate the original before the
new content lands, and a full disk then leaves a shredded task file; the swap
goes through a temporary file in the same directory instead.

Symbolic links are refused outright, in both directions. A task file is
written by agents and checked out by git, so a link is a way to read — and,
on the next edit, publish — a file that was never part of the repository.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class TextFileError(Exception):
    """Raised when a file cannot be edited safely."""


def read_exact(path: Path) -> str:
    """Read strictly, preserving line endings.

    Decoding with ``errors="replace"`` and writing back would turn any byte
    that is not valid UTF-8 into a permanent U+FFFD.
    """
    _refuse_links(path)
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except UnicodeDecodeError as error:
        raise TextFileError(
            f"{path.name} is not valid UTF-8; refusing to edit it"
        ) from error


def replace_if_unchanged(path: Path, updated: str, expected: str) -> None:
    """Swap in ``updated``, but only if the file still holds ``expected``."""
    if read_exact(path) != expected:
        raise TextFileError(f"{path.name} changed on disk — reload and try again")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(updated)
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _refuse_links(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise TextFileError(f"{path.name} is a symbolic link; refusing to touch it")
