"""Discover sibling projects that each have their own tasks folder.

When ``tv`` is run from a folder that is not itself a project (e.g. a workspace
root like ``~/gimle``), it lists the immediate child directories that contain a
tasks folder so you can step into each one.

Such a workspace is usually mostly *worktrees* — one repository checked out
many times — so the projects are also grouped: each linked worktree nests under
the repository it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass
class ProjectGroup:
    """A repository and the linked worktrees checked out from it."""

    project: Project
    worktrees: list[Project] = field(default_factory=list)

    @property
    def has_worktrees(self) -> bool:
        return bool(self.worktrees)


def group_projects(projects: list[Project]) -> list[ProjectGroup]:
    """Nest every linked worktree under the repository it belongs to.

    A worktree whose repository is not itself in the list stays at top level —
    it is still a project worth opening. Order is otherwise preserved.
    """
    by_path = {project.path: project for project in projects}
    children: dict[Path, list[Project]] = {}
    for project in projects:
        parent = _worktree_parent(project.path)
        if parent is not None and parent != project.path and parent in by_path:
            children.setdefault(parent, []).append(project)

    nested = {child.path for kids in children.values() for child in kids}
    return [
        ProjectGroup(project, children.get(project.path, []))
        for project in projects
        if project.path not in nested
    ]


def _worktree_parent(path: Path) -> Path | None:
    """The repository a linked worktree belongs to, or ``None``.

    ``git worktree add`` writes ``.git`` as a file holding
    ``gitdir: <repo>/.git/worktrees/<name>``. Reading it costs nothing, so the
    tree can be built before any git command runs. A submodule also has a
    ``.git`` file, but it points into ``.git/modules/`` and is not a worktree.
    """
    pointer = path / ".git"
    if not pointer.is_file():
        return None
    try:
        text = pointer.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    git_dir = text[len("gitdir:"):].strip()
    parent, separator, _ = git_dir.partition("/.git/worktrees/")
    if not separator:
        return None
    resolved = Path(parent)
    if not resolved.is_absolute():
        resolved = (path / parent).resolve()
    return resolved
