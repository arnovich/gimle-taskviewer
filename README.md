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
projects · gimle — 7 repos · 9 worktrees · → to open
────────────────────────────────────────────────────
  ▸ gimle-asgard  13 active
    3 wt ✎1 ✔3⚠ · ⎇ main ✎5 1h
    gimle-bifrost  3 active
    ⎇ main ✎3 4w
  ▾ gimle-mimir  36 active
    ⎇ main ✎21 9m
      166  ↑2 ↓3 18m
      453  ✔merged ✎7 51m
      task-140  ↑2 ↓224 3w
```

Press `space` to fold and unfold the highlighted repo (`▸` collapsed, `▾`
expanded). The key is only offered on a repo that actually has worktrees.

A **collapsed** repo leads its second line with what is folded away, because
that is what survives a narrow pane:

* `3 wt` — three worktrees hidden
* `✎1` — one of them has uncommitted work
* `✔3` — three are fully merged and can be removed…
* `✔3⚠` — …but at least one of those *also* holds uncommitted files, which
  exist nowhere else. Removing it would lose them.

An **expanded** worktree is listed by its folder suffix rather than its branch —
`gimle-mimir-166` shows as `166` — because that is what you `cd` to, what
`git worktree remove` takes, and where this workspace puts the task number. The
branch is in the right-hand pane.

* `✔merged` — the base branch already holds every commit here
* `+n` / `-n` — commits ahead of / behind **the base branch**
* `↑n` / `↓n` — commits to **push** / to **pull** from the remote
* `new` — never pushed; this branch exists only on this machine
* `gone` — was pushed, then deleted upstream; the work landed and was tidied up
* `✎n` — uncommitted changes in the working tree
* trailing `9m` / `3w` — how long ago the checkout was last touched

Two distances, two notations. `+n`/`-n` is how far you are from `main`;
`↑n`/`↓n` is how far you are from the remote. They are deliberately different
shapes rather than four arrows — the doubled arrows `⇡⇣` are absent from almost
every terminal font.

A row says nothing about drift when there is nothing to say: a plain clone
sitting in sync with its remote just shows its branch and age.

## Keeping up with the remote

When `tv` starts it fetches every repository in the background — one fetch per
repo, since a worktree shares its repo's object store — so a `main` that moved
while you were away shows up as `↓3` without you having to ask. The list paints
first and the counts arrive a second later; the network never blocks the UI,
and an unreachable remote fails immediately rather than waiting out a timeout.

| Key | Action |
| --- | ------ |
| `f` | Fetch every repo again now |
| `u` | Fetch, then fast-forward the highlighted project |

`u` fetches first — "already up to date" measured against hour-old refs is
worthless — then fast-forwards. It will not create a merge commit and cannot
leave you with conflicts. It declines, and says why, in every case it is not
certain:

```
proj: fast-forwarded 3 commits
proj: 5 uncommitted files — commit or stash first
proj: would overwrite ignored .env — move them aside first
proj: tracks `origin/main`, not a branch of its own —
      updating would move it onto that branch
proj: origin/feat/x no longer exists on the remote
```

That third one is not theoretical: `--ff-only` protects *tracked* files, but
git will silently overwrite a file it has been told to ignore, and a local
`.env` lives nowhere else. The fourth matters in a worktree-per-task workspace —
`git worktree add -b` inherits the upstream of the branch it was cut from, so a
task branch commonly tracks `origin/main`, and "updating" it would move it onto
main and lose what it was branched for. `tv` refuses instead.

When it does succeed the task list reloads, since the tasks arrived with the
commits.

The right pane always says when the remote was last contacted:

```
- **Remote** `origin/main` · **3 commits to pull** — press `u` · checked 2h ago
- **Remote** `origin/main` · up to date · **could not reach the remote** — counts may be stale
```

That "checked" is doing real work: `up to date` can only ever mean *as of the
last fetch*. A **failed** fetch still updates git's own `FETCH_HEAD` timestamp,
so `tv` tracks which remotes it actually reached rather than trusting that file.

## Pull requests

Under the `grind` loop each worktree becomes one pull request, so the PR lives
on the worktree row. Its number shows there — green when it would merge
cleanly, yellow when it would not:

```
  ▾ gimle-mimir  37 active
    ⎇ main ✎21 9m
      task-140 #453 +2 -268 3w
```

Highlighting it puts the PR in the right-hand pane:

```
## Pull request #453

**Task 140: derivative/phase-space augmented encoder input features**

- **Checks** 2/2 passing
- **Merge** ⚠ has conflicts with main — `M` merges anyway

### Description
...

### 4 comments

- **erikarne** — Add the 512-case benchmark before this lands.

*`m` to reply.*
```

Comments from bots — deploy previews, CI summaries — are filtered out; they post
on every PR and would bury the one comment that is review feedback.

`m` opens `$EDITOR` with a template, the way `git commit` does; save and quit to
post, leave it empty to abort.

`M` merges, with a merge commit. It asks first — the dialog names the PR, its
checks and anything that would block it:

```
┌ Merge #453 into main? ─────────────────────────┐
│   Task 140: derivative-augmented token features│
│   gimle-mimir-task-140                         │
│   checks: 2/2 passing                          │
│   ⚠  has conflicts with main                   │
│                                                │
│   y  merge     a  admin merge     esc  cancel  │
└────────────────────────────────────────────────┘
```

Nothing stops you merging a red PR. `main` is unprotected in these repos, so a
plain merge already goes through and pretending otherwise would be theatre —
the dialog tells you what is wrong and the decision stays yours. `a` adds
`--admin`, which matters only if branch protection is ever turned on.

## Keys

| Key        | Action                                             |
| ---------- | -------------------------------------------------- |
| `↑` / `↓`  | Move in the list / scroll the markdown             |
| `j` / `k`  | Move down / up in the list                         |
| `→` / `←`  | Enter a project / step back to the project list (workspace mode) |
| `space`    | Fold or unfold a repo's worktrees (workspace mode)  |
| `f`        | Fetch all repos from their remotes (workspace mode) |
| `u`        | Fast-forward the highlighted project (workspace mode) |
| `M`        | Merge the highlighted worktree's pull request       |
| `m`        | Comment on it (opens `$EDITOR`)                     |
| `Tab`      | Switch focus between the two panes                 |
| `c`        | **Work on the task with Claude Code** (see below)  |
| `R`        | **Review all tasks** with Claude Code, in the background (see below) |
| `n`        | Queue the task next (append to the work queue)      |
| `p`        | Queue the task **first** (front of the queue)       |
| `N`        | Take the task out of the queue                      |
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

## The work queue

`priority` says how important a task is. **`next:` says what to do first** — it
is a running order you set, and agents never write it. Queued tasks lead the
list, in rank order, with the rank shown in cyan:

```
  ○ 053  1  Batched GPU simulation
  ○ 051  2  Circuit completion via mutation MCTS
  ○ 050     Differentiable finetuning (replace REINFORCE)
  ○ 061     Investigate model architecture
```

`n` appends the selected task to the queue, `p` puts it at the front, `N` takes
it out. Ranks are stored as `next: <integer>` in the task's frontmatter, so the
queue travels with the repo and any agent can read it.

Gaps are fine — finishing task `1` leaves `2` and `3` where they are, and a
closed task's rank stops counting. Promoting takes the slot below the current
leader where there is one, so it usually rewrites a single file rather than
renumbering the queue.

Agents pick tasks up with the `grind` skill, which claims a task on `main`
before starting so two agents never take the same one. See
`gimle-skills/references/task-format.md` for the full task standard.

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
