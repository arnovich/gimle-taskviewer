"""Launch a headless Claude Code pass that grooms the task tracker.

The agent reviews every task, reconciles each file's ``state:`` frontmatter with
the folder it lives in, moves mis-filed tasks, sets sensible priorities, and
cleans up duplicates/stale tasks — directly on disk. This module just builds the
prompt and runs the command; the UI decides when to call it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# The default is deliberately autonomous: it runs unattended on the user's own
# task files, and every change is git-reversible.
DEFAULT_GROOM_CMD = ("claude", "-p", "--dangerously-skip-permissions")


@dataclass
class GroomResult:
    returncode: int
    output: str
    log_path: Path | None


def build_groom_prompt(folder_name: str = "tasks") -> str:
    """The instruction handed to Claude Code for a grooming pass."""
    return f"""\
You are grooming the task tracker for this project. Make the changes directly \
on disk — do the work, don't just describe it.

Task layout: tasks live under `{folder_name}/` in three subfolders that encode \
each task's state:
- `{folder_name}/open/`     — not started
- `{folder_name}/ongoing/`  — in progress
- `{folder_name}/closed/`   — done or abandoned

Each task is a markdown file (a few are directories of fragments such as \
description.md / spec.md / plan.md). Files may carry YAML frontmatter with: \
title, state, labels, priority (high | medium | low). The FOLDER is the source \
of truth for state.

Do the following:
1. Read every task in all three subfolders.
2. Fix each file's `state:` frontmatter to match the folder it is in \
(open/ongoing/closed, lowercase); add the field if missing.
3. Move any task that is clearly in the wrong folder to the right one — use \
`git mv` if this is a git repo, otherwise `mv` — and update its `state:` to \
match. A task whose body shows the work is finished belongs in `closed/`.
4. Set a sensible `priority:` (high | medium | low) on each open/ongoing task \
from its content, blockers and dependencies; add it if missing, correct it if \
clearly wrong, and leave already-reasonable priorities alone.
5. Normalise broken metadata: ensure every file has a `title:` (derive it from \
the first heading if missing) and tidy malformed frontmatter.
6. Clean up: merge duplicate tasks (keep the richer one, fold in anything \
unique, move the redundant one to `closed/` with a one-line note); move stale \
or superseded tasks to `closed/` with a short reason. Prefer moving to \
`closed/` over deleting — only delete a file that is empty or pure noise.

Constraints:
- Do NOT invent new tasks or change the substance of a task's requirements.
- Preserve the author's wording and formatting — edit frontmatter and filing, \
not intent.
- Keep filenames stable (including any numeric prefix) unless you are merging.

When finished, print a concise summary: one line per changed task as \
`id: what you changed and why`. If nothing needed changing, say so.
"""


def run_groom(
    groom_cmd: list[str],
    project_root: Path,
    folder_name: str,
    *,
    log_path: Path | None = None,
    capture: bool = True,
) -> GroomResult:
    """Run the grooming command in ``project_root``.

    With ``capture`` the agent's output is collected (and written to
    ``log_path`` if given) and returned; without it, output streams straight to
    the inherited terminal — used by the headless ``--groom`` CLI path.
    """
    prompt = build_groom_prompt(folder_name)
    cmd = [*groom_cmd, prompt]

    if not capture:
        proc = subprocess.run(cmd, cwd=project_root)
        return GroomResult(proc.returncode, "", None)

    proc = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True)
    output = proc.stdout
    if proc.stderr:
        output = f"{output}\n\n[stderr]\n{proc.stderr}" if output else proc.stderr
    if log_path is not None:
        log_path.write_text(output, encoding="utf-8")
    return GroomResult(proc.returncode, output, log_path)
