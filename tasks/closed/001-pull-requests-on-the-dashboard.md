---
title: Show each task's pull request on the control plane
state: closed
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

## Notes

Done on 2026-09-24, together with CI state. Open pull requests come from `gh pr list` and workflow runs from `gh run list`, asked for on their own lazy cadence (three minutes, ten after an error) beside the git fetch, never more than once per repo per page load, and a `gh` that is missing or logged out degrades to "could not be asked". The dashboard counts pull requests that are ready for review under Needs you (drafts and changes-requested ones are listed as the agent's move), the task page shows its PR with checks in the meta line, the Running-now cards link each held task's PR, and the sidebar gives each repo a health dot for its default branch.
