# CLAUDE.md

`gimle-taskviewer` is two front-ends over the same markdown task files:

- **`tv`** — the terminal UI (`src/task_viewer/app.py`): browse a project or a
  workspace of worktrees, rank the queue, review and merge pull requests.
- **`tv-web`** — the control plane (`src/task_viewer/web/`): a thin web app
  that runs from the repositories alone, keeping its own clones
  (`src/task_viewer/mirror.py`), showing who is waiting for an answer and what
  is being worked on, and writing back answers and queue order as commits.
  Design in `docs/control-plane.md`.

Both read tasks through `discovery.py` and write them through the atomic
helpers in `textfile.py`; `conversation.py` is the reader and writer for the
`## Conversation` thread; `queue_ops.py` owns `next:`.

## Commands

```sh
uv sync                      # deps, including dev
uv run pytest -q             # the whole suite drives real git repos; ~1 min
uv run tv ~/gimle            # the TUI on the workspace
uv run tv-web --repo <url>   # the control plane, clones into ~/.local/share/tv/mirrors
```

Tests never touch the developer's git config (see `tests/conftest.py`) and
build throwaway repositories rather than faking the git CLI.

## Conventions

- Python 3.11+, type hints, docstrings on modules and public functions.
- Never commit directly to `main`; work on a branch in a worktree
  (`git worktree add ../gimle-taskviewer-<name> -b <type>/<name>`).
- No `Co-Authored-By` lines.
- Every write to a task file goes through `textfile.replace_if_unchanged`,
  because these files have other writers (agents, the groom pass, the owner's
  editor).

### Task File Format

Task files follow **`gimle-skills/references/task-format.md`** — that document is
the authority on frontmatter, filenames, body sections and priority meanings.
Read it before creating or editing a task file.

The short version: filename `NNN-kebab-slug.md` where the number is the task's
identity and never changes; required frontmatter `title`, `state` (lowercase
`open`/`ongoing`/`closed`), `priority`, `labels`; required body sections
`## Context` and a checkable `## Outcome`; no invented frontmatter fields.

`next:` is the owner's work queue, set in `tv`. **Agents never write it.**
