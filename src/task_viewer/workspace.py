"""Discover sibling projects that each have their own tasks folder.

When ``tv`` is run from a folder that is not itself a project (e.g. a workspace
root like ``~/gimle``), it lists the immediate child directories that contain a
tasks folder so you can step into each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .discovery import is_tasks_dir


@dataclass
class Project:
    """A single project: its display name, root path, and tasks folder."""

    name: str
    path: Path
    tasks_dir: Path


def find_projects(start: Path, folder_name: str = "tasks") -> list[Project]:
    """Immediate child directories of ``start`` that hold a tasks folder.

    Sorted by name. Hidden directories (dot-prefixed) are skipped.
    """
    start = start.resolve()
    if not start.is_dir():
        return []
    projects: list[Project] = []
    for child in sorted(start.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        tasks = child / folder_name
        if is_tasks_dir(tasks):
            projects.append(Project(child.name, child, tasks))
    return projects
