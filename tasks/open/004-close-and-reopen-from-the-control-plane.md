---
title: Close and reopen a task from the control plane
state: open
priority: low
labels: [control-plane, enhancement]
---

# Close and reopen a task from the control plane

## Context

`tv` moves tasks between `open/`, `ongoing/` and `closed/` with `x`, `c` and
`r`; the control plane cannot. The owner mostly needs two of those moves:
close a task that is done or abandoned, and reopen one that was closed too
soon. Claiming stays with the agents.

## Outcome

- *Close* and *Reopen* buttons on the task page, each a `git mv` plus the
  `state:` change, pushed as one commit (`task NNN: closed` / `reopened`).
- Closing an `ongoing` task drops `claimed_by`, `claimed_at` and `branch`
  and leaves `next:` alone, exactly as the standard says.
- The dashboard's counts reflect the move on the next load without a forced
  refresh.
