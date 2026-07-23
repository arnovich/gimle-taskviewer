"""Shared fixtures: build a throwaway project tree with tasks/open|closed."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root containing a representative tasks/ folder."""
    root = tmp_path / "gimle-example"

    _write(
        root / "tasks" / "open" / "052-heat-equation.md",
        "---\n"
        'title: "2D heat equation"\n'
        "state: OPEN\n"
        "labels: [enhancement, runtime]\n"
        "priority: low\n"
        "---\n\n"
        "# 2D Heat Equation\n\nSome body text.\n",
    )
    _write(
        root / "tasks" / "open" / "003-no-frontmatter.md",
        "# Plain task\n\nNo frontmatter here, title from heading.\n",
    )
    _write(
        root / "tasks" / "closed" / "001-done.md",
        "---\ntitle: Done thing\nstate: closed\npriority: high\n---\n\nClosed body.\n",
    )
    # Directory-style task (bifrost shape).
    _write(
        root / "tasks" / "open" / "010-dir-task" / "description.md",
        "---\ntitle: Directory task\nlabels: [multi]\n---\n\nDescription part.\n",
    )
    _write(
        root / "tasks" / "open" / "010-dir-task" / "plan.md",
        "The plan part.\n",
    )
    return root
