# tv — task viewer

A terminal UI for browsing a project's markdown task files.

## Task layout

`tv` expects a project to keep its tasks under a `tasks/` folder split into
`open/` and `closed/`:

```
my-project/
└── tasks/
    ├── open/
    │   ├── 052-2d-heat-equation.md
    │   └── 081-multi-agent-monitor.md
    └── closed/
        └── 001-initial-bug.md
```

Each task is a markdown file, optionally with YAML frontmatter:

```markdown
---
title: "2D heat equation (spatial PDE support)"
state: open
labels: [enhancement, runtime]
priority: low
---

# 2D Heat Equation

Body of the task...
```

Tasks move through three states, one per subfolder: `open/` → `ongoing/` →
`closed/`. `tv` shows the active ones (open + ongoing) by default.

Everything except the file itself is optional: if there's no frontmatter, the
title falls back to the first `# heading` and then to the filename. A task can
also be a *directory* of markdown fragments (`description.md`, `spec.md`,
`plan.md`, ...) — those are concatenated into a single view.

`tv` shows all of this in a two-pane TUI: the task list on the left, the
rendered markdown on the right.

## Install

```sh
uv tool install .
```

This puts a `tv` command on your PATH.

## Usage

```sh
cd my-project
tv               # finds the nearest enclosing tasks/ folder
tv ../other-project   # or point it somewhere explicitly
tv -f issues     # look for an issues/ folder instead of tasks/
```

`tv` walks up from the given directory (default: the current one) to the
nearest `tasks/` folder, so it works from anywhere inside a project. Use
`-f`/`--folder NAME` if your project calls that folder something other than
`tasks` (it still needs `open/` and `closed/` subfolders).

## Keys

| Key        | Action                                             |
| ---------- | -------------------------------------------------- |
| `↑` / `↓`  | Move in the list / scroll the markdown             |
| `j` / `k`  | Move down / up in the list                         |
| `Tab`      | Switch focus between the two panes                 |
| `c`        | **Work on the task with Claude Code** (see below)  |
| `g`        | Mark the task ongoing                              |
| `x`        | Mark the task done (move to `closed/`)             |
| `u`        | Reopen the task (move back to `open/`)             |
| `o`        | Toggle showing closed tasks (active-only by default) |
| `r`        | Reload tasks from disk                             |
| `q`        | Quit                                               |

Task priority is colour-coded in the list (high = red, medium = yellow,
low = dim); `○` marks an open task, `◐` ongoing, `●` closed.

## Working on a task with Claude Code

Press `c` on a task and `tv` will:

1. Mark it **ongoing** (move it to `tasks/ongoing/` and sync its `state:`
   frontmatter), then
2. suspend the TUI and launch [Claude Code](https://claude.com/claude-code) in
   the project root, seeded with a prompt that points at the task spec.

When you exit Claude Code you drop straight back into `tv`. If the task is
finished, press `x` to mark it done.

The launch command defaults to `claude`. Override it with `--claude-cmd` or the
`TV_CLAUDE_CMD` environment variable, e.g. to pin a model:

```sh
tv --claude-cmd "claude --model opus"
```

## Development

```sh
uv sync
uv run pytest
uv run tv path/to/project
```
