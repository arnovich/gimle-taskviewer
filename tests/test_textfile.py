"""Tests for the shared read-exactly / replace-if-unchanged discipline."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from task_viewer.textfile import TextFileError, read_exact, replace_if_unchanged


def test_replace_refuses_when_someone_wrote_in_between(tmp_path: Path) -> None:
    md = tmp_path / "f.md"
    md.write_text("one\n", encoding="utf-8")
    snapshot = read_exact(md)
    md.write_text("one\nclaimed_by: grind-7\n", encoding="utf-8")  # the other writer
    with pytest.raises(TextFileError, match="changed on disk"):
        replace_if_unchanged(md, "two\n", expected=snapshot)
    assert "grind-7" in md.read_text(encoding="utf-8")
    assert list(tmp_path.iterdir()) == [md]  # no temp file left behind


def test_invalid_utf8_is_refused_rather_than_mangled(tmp_path: Path) -> None:
    md = tmp_path / "f.md"
    md.write_bytes(b"caf\xe9\n")
    with pytest.raises(TextFileError, match="not valid UTF-8"):
        read_exact(md)


def test_symlinks_are_refused_in_both_directions(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("hunter2\n", encoding="utf-8")
    link = tmp_path / "task.md"
    link.symlink_to(secret)
    with pytest.raises(TextFileError, match="symbolic link"):
        read_exact(link)
    with pytest.raises(TextFileError, match="symbolic link"):
        replace_if_unchanged(link, "x\n", expected="hunter2\n")
    assert secret.read_text(encoding="utf-8") == "hunter2\n"

    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "t.md").write_text("x\n", encoding="utf-8")
    linked_dir = tmp_path / "tasks"
    linked_dir.symlink_to(real_dir)
    with pytest.raises(TextFileError, match="symbolic link"):
        read_exact(linked_dir / "t.md")


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o664])
def test_replace_keeps_the_file_mode(tmp_path: Path, mode: int) -> None:
    md = tmp_path / "f.md"
    md.write_text("one\n", encoding="utf-8")
    os.chmod(md, mode)
    replace_if_unchanged(md, "two\n", expected="one\n")
    assert stat.S_IMODE(os.stat(md).st_mode) == mode
    assert md.read_text(encoding="utf-8") == "two\n"


def test_a_failed_swap_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    md = tmp_path / "f.md"
    md.write_text("one\n", encoding="utf-8")

    def explode(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        replace_if_unchanged(md, "two\n", expected="one\n")
    assert md.read_text(encoding="utf-8") == "one\n"
    assert list(tmp_path.iterdir()) == [md]
