---
title: Show each task's pull request on the control plane
state: open
priority: medium
labels: [control-plane, enhancement]
---

# Show each task's pull request on the control plane

## Context

`tv` already finds the PR for a worktree (`pull_requests.py`, via `gh`) and
shows its checks, mergeability and comments. The control plane has no
worktrees, but an ongoing task carries its `branch:` in frontmatter, and a
closed task's PR is what the owner most wants to see next to it. Today the
dashboard stops at "claimed by X on branch Y".

## Outcome

- The task page shows the open PR for the task's `branch` (number, title,
  checks, mergeable, link), found through `gh pr list --head <branch>`.
- The dashboard's *In progress* rows link to that PR when one exists.
- `gh` being absent or unauthenticated degrades to "no PR information", not
  an error page.
- Lookups are cached per refresh so a dashboard load makes at most one `gh`
  call per repo.
