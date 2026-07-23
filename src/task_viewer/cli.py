"""Command-line entry point for ``tv``."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from .app import TaskViewerApp
from .discovery import TasksNotFoundError, find_tasks_dir


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
    args = parser.parse_args(argv)

    claude_cmd = shlex.split(args.claude_cmd)
    if not claude_cmd:
        print("tv: --claude-cmd is empty", file=sys.stderr)
        return 2

    start = Path(args.path).expanduser()
    if not start.exists():
        print(f"tv: path does not exist: {start}", file=sys.stderr)
        return 2

    try:
        tasks_dir = find_tasks_dir(start, args.folder)
    except TasksNotFoundError as error:
        print(f"tv: {error}", file=sys.stderr)
        return 1

    project_name = tasks_dir.parent.name
    TaskViewerApp(tasks_dir, project_name, claude_cmd).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
