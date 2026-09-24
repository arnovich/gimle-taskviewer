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
- Unlike the control plane this writes to the working tree only — committing
  stays with the owner, as it does for `next:`.
