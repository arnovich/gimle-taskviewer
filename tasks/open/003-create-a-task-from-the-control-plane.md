---
title: Create a task from the control plane
state: open
priority: medium
labels: [control-plane, enhancement]
---

# Create a task from the control plane

## Context

Filing a task today means an editor and a commit. The control plane can do it
as one more metadata commit: allocate the next number, write the file in the
standard shape, push. Number allocation is the only subtle part — the
standard forbids reusing numbers and requires refusing an ambiguous one, and
`gimle-asgard` has a parallel `review-NNN-` namespace that collides with
plain numbers.

## Outcome

- A *New task* form on the repo page: title, priority, labels, context,
  outcome. It writes `tasks/open/NNN-slug.md` with the required frontmatter
  and sections and pushes it as `task NNN: filed`.
- `NNN` is one more than the highest number across `open/`, `ongoing/` and
  `closed/`, counting every `NNN-` and `review-NNN-` prefix.
- A push rejected because someone else allocated the same number retries
  with a fresh number, not the same one.
