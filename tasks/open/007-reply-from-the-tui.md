---
title: Reply to a task's conversation from tv
state: open
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
