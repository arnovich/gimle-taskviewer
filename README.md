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
tv ~/code        # a workspace root -> browse projects (see below)
```

`tv` walks up from the given directory (default: the current one) to the
nearest `tasks/` folder, so it works from anywhere inside a project. Use
`-f`/`--folder NAME` if your project calls that folder something other than
`tasks` (it still needs `open/` and `closed/` subfolders).

## Browsing a workspace of projects

If you run `tv` from a folder that has no tasks of its own but whose child
folders are projects (e.g. `~/code` containing many repos), the left pane lists
those child projects instead — with their active-task counts and, for anything
that is a git checkout, its branch, drift and freshness.

Worktrees are **folded under the repository they came from**, so a workspace of
30 checkouts reads as a handful of repos. A collapsed repo still says what it is
hiding:

```
projects · gimle — 7 repos · 12 worktrees · → to open
────────────────────────────────────────────────────
  ▸ gimle-asgard  13 active
    ⎇ main ✎5 35m · 3 wt · 3 merged
    gimle-bifrost  3 active
    ⎇ main ✎3 4w
  ▾ gimle-mimir  36 active
    ⎇ main ✎21 9m
    ⎇ fix/rewrite_api… ↑2 ↓3 18m
    ⎇ test/merge_453 ✔merged ✎7 51m
    ⎇ task/140-deriva… ↑2 ↓224 3w
```

Press `space` to fold and unfold the highlighted repo (`▸` collapsed, `▾`
expanded). `3 wt · 3 merged` means three worktrees, all of them already merged
into the base branch — cleanup waiting, visible without expanding anything.

* `⎇ branch` — the checked-out branch (truncated to fit)
* `✔merged` — **the base branch already holds every commit here**; the worktree
  has served its purpose and can be removed
* `↑n` / `↓n` — commits ahead of / behind the base branch
* `✎n` — uncommitted changes in the working tree
* trailing `8m` / `3w` — how long ago the checkout was last touched

A row says nothing about drift when there is nothing to say: a plain clone
sitting in sync with its remote just shows its branch and age.

Press `→` (or `Enter`) to step into a project — a repo or one of its worktrees —
and see its task list; press `←` to step back out. Everything else works the
same once you're inside.

Grouping is read from each worktree's `.git` pointer file, which costs nothing,
so the tree is correct the instant the list paints. The git *state* is scanned
in a background thread and fills in a moment later.

## Keeping track of worktrees

A workspace like `~/gimle` is mostly *worktrees* — one repository checked out
many times, one branch per line of work. Highlighting a project shows the full
picture in the right pane:

```
## Worktree

`task/140-derivative-augmented-token-features` · worktree of `gimle-mimir`

- **Created** 2026-07-30 00:22 (3w ago)
- **Updated** 2026-07-30 00:53 (3w ago)
- **Base** `main` · **2 ahead** · 221 behind

### 2 commits not in `main`

- Address panel review: scope version bump, sanitize derivatives, tests
- Add derivative/phase-space augmented encoder input features (task 140)
```

* **Created** — when `git worktree add` made this worktree. A plain clone has no
  such marker, so it reports **Branch since** instead: when the current branch
  started, per the reflog. If the reflog has been trimmed by `gc.reflogExpire`
  its oldest entry is just an old `pull`, not a birth — that reports `unknown`
  rather than a confidently wrong date.
* **Updated** — the most recent of the last commit and any uncommitted edit, so
  a worktree with live but uncommitted work doesn't read as stale.
* **Base** — drift against the first of `main`, `origin/main`, `master`,
  `origin/master` that exists and isn't the branch you're standing on. Sitting
  on `main` therefore compares against `origin/main`, showing unpushed work.
  Fully-qualified refs are used, so a *tag* named `main` can't skew the counts.

When a worktree is fully merged the pane says so instead of listing drift — and
if it still holds uncommitted files, it warns, because those exist nowhere else
and removing the worktree would lose them:

```
- **Merged** fully into `main` — nothing of its own left

> ⚠ Merged, but 10 files here are uncommitted — they exist nowhere else.
```

A detached HEAD (mid-rebase, mid-bisect, on a tag) is shown as
`detached at abc1234` rather than hidden.

Only child folders that have a tasks folder are listed, so a worktree without
one won't appear.

## Keys

| Key        | Action                                             |
| ---------- | -------------------------------------------------- |
| `↑` / `↓`  | Move in the list / scroll the markdown             |
| `j` / `k`  | Move down / up in the list                         |
| `→` / `←`  | Enter a project / step back to the project list (workspace mode) |
| `space`    | Fold or unfold a repo's worktrees (workspace mode)  |
| `Tab`      | Switch focus between the two panes                 |
| `c`        | **Work on the task with Claude Code** (see below)  |
| `R`        | **Review all tasks** with Claude Code, in the background (see below) |
| `g`        | Mark the task ongoing                              |
| `x`        | Mark the task done (move to `closed/`)             |
| `u`        | Reopen the task (move back to `open/`)             |
| `o`        | Toggle showing closed tasks (active-only by default) |
| `r`        | Reload tasks from disk                             |
| `q`        | Quit                                               |

Task priority is colour-coded in the list (high = red, medium = yellow,
low = dim); `○` marks an open task, `◐` ongoing, `●` closed.

The leading id number of the filename is shown next to the state mark, so
`052-2d-heat-equation.md` lists as `○ 052 2D heat equation`. Tasks that have no
number are padded to the same width, keeping every title in one column:

```
  ○ 030 Bash sandbox — remaining cross-backend work
  ○ 032 Bash sandbox — per-agent persistent volumes
  ○     Bash sandbox — roadmap index
```

A project whose tasks are all unnumbered gets no such column at all.

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

## Reviewing all tasks in the background

Press `R` and `tv` launches a **headless** Claude Code pass over the whole
tracker while you keep browsing (the subtitle shows `⟳ reviewing…`). The agent
reconciles each file's `state:` with its folder, moves mis-filed tasks between
`open`/`ongoing`/`closed`, sets sensible priorities, and merges or closes stale
duplicates — directly on disk. When it finishes, the list reloads and its
summary appears in the right pane.

By default this runs `claude -p --dangerously-skip-permissions` so the agent can
edit and move files unattended. It operates only on your task files and every
change is git-reversible — **run it in a git repo** so you can review the diff
(`git diff`) and undo with `git checkout` if needed. Override the command with
`--groom-cmd` or `$TV_GROOM_CMD`.

You can also run one pass from the shell without opening the TUI:

```sh
tv --groom            # review this project's tasks and exit
```

## Development

```sh
uv sync
uv run pytest
uv run tv path/to/project
```
