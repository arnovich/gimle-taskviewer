"""Command-line entry point for ``tv``."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

from .app import TaskViewerApp
from .discovery import TasksNotFoundError, find_tasks_dir
from .groom import DEFAULT_GROOM_CMD, run_groom
from .workspace import find_projects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tv",
        description="Browse a gimle project's markdown tasks in a terminal UI.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Project directory to start from (default: current directory). "
        "The nearest enclosing tasks folder is used.",
    )
    parser.add_argument(
        "-f",
        "--folder",
        default="tasks",
        metavar="NAME",
        help="Name of the tasks folder to look for (default: tasks).",
    )
    parser.add_argument(
        "--claude-cmd",
        default=os.environ.get("TV_CLAUDE_CMD", "claude"),
        metavar="CMD",
        help="Command used to launch Claude Code on a task "
        "(default: claude, or $TV_CLAUDE_CMD).",
    )
    parser.add_argument(
        "--groom-cmd",
        default=os.environ.get("TV_GROOM_CMD", " ".join(DEFAULT_GROOM_CMD)),
        metavar="CMD",
        help="Command used for the background task review "
        "(default: 'claude -p --dangerously-skip-permissions', or $TV_GROOM_CMD).",
    )
    parser.add_argument(
        "--groom",
        action="store_true",
        help="Run one grooming pass headlessly (no TUI) and exit.",
    )
    args = parser.parse_args(argv)

    claude_cmd = shlex.split(args.claude_cmd)
    groom_cmd = shlex.split(args.groom_cmd)
    if not claude_cmd:
        print("tv: --claude-cmd is empty", file=sys.stderr)
        return 2
    if not groom_cmd:
        print("tv: --groom-cmd is empty", file=sys.stderr)
        return 2

    start = Path(args.path).expanduser()
    if not start.exists():
        print(f"tv: path does not exist: {start}", file=sys.stderr)
        return 2

    # A project encloses `start` (single-project mode), or `start` is a
    # workspace whose child folders are projects (project-browser mode).
    try:
        tasks_dir = find_tasks_dir(start, args.folder)
    except TasksNotFoundError:
        tasks_dir = None

    if tasks_dir is not None:
        if args.groom:
            return _run_groom_headless(groom_cmd, tasks_dir)
        project_name = tasks_dir.parent.name
        app = TaskViewerApp.single(tasks_dir, project_name, claude_cmd, groom_cmd)
        app.run()
        return 0

    projects = find_projects(start, args.folder)
    if not projects:
        print(
            f"tv: no {args.folder}/ folder found from {start.resolve()}, "
            f"and no child project has one either.",
            file=sys.stderr,
        )
        return 1
    if args.groom:
        print("tv: --groom must be run from inside a project", file=sys.stderr)
        return 2

    TaskViewerApp(
        projects, start.resolve().name, workspace=True,
        claude_cmd=claude_cmd, groom_cmd=groom_cmd,
    ).run()
    return 0


def _run_groom_headless(groom_cmd: list[str], tasks_dir: Path) -> int:
    if shutil.which(groom_cmd[0]) is None:
        print(f"tv: {groom_cmd[0]!r} not found on PATH", file=sys.stderr)
        return 1
    print(f"Reviewing tasks in {tasks_dir} with: {' '.join(groom_cmd)}\n")
    result = run_groom(groom_cmd, tasks_dir.parent, tasks_dir.name, capture=False)
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
