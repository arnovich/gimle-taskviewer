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

| Page | Shows | Writes |
|---|---|---|
| `/` | **Needs you** — every task whose thread ends in a question you have not answered, oldest first. **In progress** — every `ongoing` task, who claimed it, on which branch, since when. **Repositories** — counts, the queue per repo, when each remote was last reached. | Refresh |
| `/r/<repo>` | The repo's active tasks (closed on request), with rank, priority, labels and the `?` marker | — |
| `/r/<repo>/t/<id>` | The task body, the conversation as entries, claim details, a link to the file on GitHub | Append an entry · Do this first / Add to queue / Remove from queue |

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

Cloning happens once, at startup. Pushes use whatever git credentials the
machine already has (`gh auth setup-git` is enough); git is run with every
prompt disabled, so a missing credential fails fast instead of hanging.

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
