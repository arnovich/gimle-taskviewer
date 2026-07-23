"""Tests for the grooming prompt and runner (using a stub agent)."""

from __future__ import annotations

import sys
from pathlib import Path

from task_viewer.groom import build_groom_prompt, run_groom

# A stand-in for `claude`: prints a fixed report, drops a marker in its cwd so we
# can prove it ran in the project root, and ignores the appended prompt arg.
_STUB = """\
import os, sys
open(os.path.join(os.getcwd(), "GROOMED"), "w").close()
sys.stdout.write("- 052: raised priority to high\\n")
sys.exit(0)
"""


def _stub_cmd(tmp_path: Path) -> list[str]:
    stub = tmp_path / "fake_claude.py"
    stub.write_text(_STUB)
    return [sys.executable, str(stub)]


def test_build_groom_prompt_mentions_layout_and_folder() -> None:
    prompt = build_groom_prompt("issues")
    assert "issues/open/" in prompt
    assert "issues/ongoing/" in prompt
    assert "issues/closed/" in prompt
    assert "priority" in prompt
    assert "git mv" in prompt


def test_run_groom_captures_output_and_writes_log(project: Path, tmp_path: Path) -> None:
    log = tmp_path / "groom.log"
    result = run_groom(
        _stub_cmd(tmp_path),
        project,  # project root
        "tasks",
        log_path=log,
    )
    assert result.returncode == 0
    assert "raised priority to high" in result.output
    assert log.read_text() == result.output
    # Ran in the project root, not the cwd of the test process.
    assert (project / "GROOMED").exists()


def test_run_groom_without_capture(project: Path, tmp_path: Path) -> None:
    result = run_groom(_stub_cmd(tmp_path), project, "tasks", capture=False)
    assert result.returncode == 0
    assert result.output == ""
    assert result.log_path is None
