# The control plane

`tv-web` is the web face of the same idea as `tv`: **you talk to agents through
tasks, never directly.** Agents pick tasks up, write plans, ask questions and
open pull requests; you rank the queue, answer the questions and merge. Nothing
about a running agent is visible — only what it wrote into a task — and that
is deliberate: a task file is the whole record, it lives in the repo, and it
outlives every session on both sides.

## Principles

1. **The repositories are the database.** There is no other state. Every
   task, every rank, every question and answer is a markdown file on the
   default branch, in the shape fixed by
   `gimle-skills/references/task-format.md`.
2. **The app owns its checkouts.** It never reads or writes your working
   directories. Each watched repo is cloned into the app's data directory
   (`~/.local/share/tv/mirrors/<name>` by default), kept on the default
   branch, always clean, always at the remote's tip as of the last fetch.
3. **Reads are lazy, writes are immediate.** A page load fetches a repo only
   if the last fetch is older than `max_age` (60s); *Refresh* forces it.
   Every write is one small commit pushed at once — the same metadata-only
   exception to "never commit to main" that a `grind` claim uses.
4. **Two writes, no more.** Answer (or note, or ask) in a task's
   `## Conversation`, and change the queue order (`next:`). Everything else
   an agent does, and everything else you do in an editor.
5. **Repo content is untrusted.** Agents write these files. Markdown renders
   with raw HTML off; a symbolic link in a repo is never followed (clones are
   made with `core.symlinks=false`, and the loader and the writer both refuse
   links anyway); an entry you type may not contain headings or an unclosed
   code fence, so it cannot read back as someone else's entry.

## How a write lands

```
POST /r/<repo>/t/<id>/reply
   │
   ▼
Mirror.commit_push(edit, "task 042: answer from erikarne")
   │  fetch + reset --hard origin/main       ← always start from the tip
   │  edit(root) → appends the entry          ← reapplied on every attempt
   │  git add <file>; git commit; git push
   │
   ├─ pushed          → done
   ├─ rejected        → an agent pushed first: reset, fetch, edit again (×3)
   └─ any other error → reset; the page says why (HTTP 409)
```

The edit callback is deliberately re-runnable: it re-reads the task from the
fresh tree each time, so a claim that landed in between is never overwritten.
Only a genuine race is retried — a push a server hook declined is reported
once, with the hook's reason. Whether anything changed is decided from the
tree, not from what the callback claims, so an edit that changed nothing
pushes nothing. One caveat is inherent: a push that times out *after* the
server accepted it reads as a failure, and retrying appends the entry twice —
read the thread before retrying a timed-out write.

## What the pages show

Every page carries a sidebar: the repositories, each with its open count and
an amber number where something there needs you; the total under *Needs you*;
when the remotes were last checked; and Refresh. A `!` marks a repo whose
remote could not be reached or could not be cloned.

| Page | Shows | Writes |
|---|---|---|
| `/` | One screen. The **Needs you** count with its breakdown, the questions agents asked in full, and the other buckets — branches ready to merge, tasks given up on, ambiguous numbers — as folds. **Running now**: one card per agent handle with what it holds, when it was last seen (its branch tip, its last entry, its claim), its last note as a status line, and a *stale* flag after four quiet hours. **Up next**: per repo, the one task grind would take, or how many are ranked and held. **Activity**: the latest dozen events. | Refresh |
| `/needs-you` | Every bucket in full, plus your own open questions waiting on an agent. | — |
| `/activity` | The last seven days across all repos, or one repo with `?repo=`: claims, plans, closes, merges, questions, answers, notes. | — |
| `/r/<repo>` | The queue as grind reads it — every ranked task with the reason it is held and the one that is next — then the tasks (closed on request) with rank, priority, labels and the `?` marker. | — |
| `/r/<repo>/t/<id>` | The task body, then its history: every commit that touched it on the default branch and every entry of its conversation, in one timeline; claim details; a link to the file on GitHub. | Append an entry · Do this first / Add to queue / Remove from queue |

All of that is read from the repositories and nothing else. The git log is the
activity log: every grind step is a commit on the default branch with a
structured subject (`task 053: claim`, `task 053: plan`, `task 053: closed`),
the control plane's own writes follow the same shape, and GitHub's merge
commits name the branch they merged, so a pull request landing is one event.
A task's remote branch is the closest thing to a heartbeat there is: the agent
works there, and every push moves the tip. Notes agents leave in the thread
become the status line on their card, which is why the grind skill should
leave one at each step of a lap.

"Needs you" is derived, never stored: the last `question` with no `answer`
after it is open, and the task waits on whoever did not ask. Who asked is
read from the handle alone — an agent's has a `/` in it (`claude/1ff2478a`),
yours does not — so an answer you typed in an editor counts the same as one
from this page. A question *you* asked shows under a separate heading,
waiting on an agent. Closed tasks are scanned too, because grind closes a
task when its PR opens and that is when you ask most.

## Running it

```sh
uv tool install .            # gives you tv and tv-web
tv-web --repo https://github.com/arnovich/gimle-mimir.git --repo ...
```

Or keep the list in `~/.config/tv/web.toml`:

```toml
owner = "erikarne"                       # written on your entries
repos = [
  "https://github.com/arnovich/gimle-mimir.git",
  "https://github.com/arnovich/gimle-asgard.git",
]
# data_dir = "~/.local/share/tv/mirrors"
# max_age = 60                           # seconds between lazy fetches
# host = "127.0.0.1"
# port = 8765
```

`--repo URL` on the command line adds to that list for one run, and `--config`
points at a different file. The list is read once, at startup: edit it and
restart. A repo new to the list is cloned then, into `data_dir/<name>`
(the last path segment of the URL, without `.git`); two URLs that end the
same way are refused, since they would share a clone. A repo dropped from
the list leaves its clone behind — delete the folder yourself. Pushes use
whatever git credentials the machine already has (`gh auth setup-git` is
enough); git is run with every prompt disabled, so a missing credential
fails fast instead of hanging.

If the port is taken, startup fails with git-free wording (`address already
in use`); set `port` in the config or pass `--port`.

It binds to localhost and has no authentication. Two things are checked
even there, because a browser on localhost still visits other sites: the
`Host` header must name this machine, so a DNS-rebinding page cannot read
the dashboard, and a POST must come from this origin (`Sec-Fetch-Site` /
`Origin`), so a page elsewhere cannot push commits as you. That is not a
login. Do not expose it beyond localhost as it is — see the tasks in
`tasks/open/` for what has to happen first.

## Where it goes next

The rest of the vision is filed as tasks in this repo's `tasks/open/`, so it
can be ranked and ground like everything else:

- pull requests on the dashboard, next to the task that produced them;
- creating and closing tasks from the page;
- **starting an agent from the page** — a sandbox or VPS booted with one
  instruction, *grind this repo*, and nothing else. The control plane never
  needs to hear from it again: everything it does comes back as commits;
- a background refresh so the dashboard stays live without a page load;
- access control, so it can leave localhost.
