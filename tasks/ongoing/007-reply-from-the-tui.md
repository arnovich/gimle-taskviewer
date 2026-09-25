---
title: Reply to a task's conversation from tv
state: ongoing
claimed_by: claude/6e99c99d-242f-4123-8681-978dfc1a3b08
claimed_at: 2026-09-25T07:48:16Z
branch: task/007_reply_from_the_tui
priority: medium
labels: [tui, enhancement]
---

# Reply to a task's conversation from tv

## Context

`tv` now marks a task waiting for an answer with `?` and names who asked, but
answering means leaving for an editor. It already has the pattern for this:
`m` opens `$EDITOR` with a template to comment on a PR. The thread should get
the same key.

## Outcome

- `a` on a task opens `$EDITOR` with a template (kind, then text) and, on
  save, appends the entry through `conversation.append` with the owner's
  handle and the current time.
- The default kind is `answer` when the thread has an open question and
  `note` otherwise; an empty file aborts.
- The list re-renders so the `?` disappears when the question is answered.
- Like the control plane, the entry is committed and pushed to origin's
  default branch on its own: that one task file only, from a throwaway
  detached worktree of `origin/main` (the checkout may be dirty or on another
  branch), retried from the fresh tip when the push is rejected. An agent
  waiting in `grind` polls `main` for the answer, so an entry that stays in
  the working tree reaches nobody. `next:` keeps its own commit rules.

## Plan

- **Approach** — `a` on a task opens `$EDITOR` on a template whose first line is
  the kind (`answer` when the thread has an open question, else `note`) and
  whose remaining lines are the text, fenced off with the same `<!-- tv:`
  marker the PR comment uses. The entry is built with `conversation.new_entry`
  under the owner's handle (`TV_OWNER`, else git `user.name` squeezed to one
  token, as the control plane does) and then **committed and pushed to
  origin's default branch on its own**: a throwaway detached worktree of
  `origin/<default>` is created next to the repo, the task's file is found
  there by number (it may have moved to `ongoing/` or `closed/` since the
  local copy was read), `conversation.append` adds the entry, that one path
  is staged, committed as `task NNN: <kind>` and pushed `HEAD:refs/heads/<default>`;
  a rejected push is retried from the fresh tip, up to three times. The
  worktree is removed whatever happens. The working tree is never written:
  a task file edited locally would block the owner's next pull (seen on
  Heimdall). Afterwards, if the checkout is on the default branch and clean,
  it is fast-forwarded with the existing `remote.fast_forward` so the list
  re-renders without the `?`; otherwise the owner is told the entry is on
  `main` and the checkout was left alone.
- **Files** — new `src/task_viewer/reply.py` (`push_entry`, `find_task_file`,
  `owner_handle`); `app.py` gains the `a` binding, `action_reply_task`, a
  generalised editor helper shared with the PR comment, and a worker that
  pushes then refreshes; `README.md` keys table and the "Talking to agents"
  section; `tests/test_reply.py` and an app test.
- **Proof** — against a real bare origin: the entry lands on origin's default
  branch in a commit touching only the task file, is found when the task has
  moved on `main`, survives a push race (origin moves between fetch and push),
  leaves no worktree registered, and the app test presses `a` with a scripted
  `$EDITOR` and sees the `?` disappear from a clean checkout on `main`.
- **Risks** — the owner's editor detaching (handled as for `m`: empty draft
  aborts, draft kept on failure); a task present locally but not on `main`
  (reported, nothing pushed); hooks in the repo running on the metadata commit
  (same exposure as an agent's claim; the commit is one markdown file).
