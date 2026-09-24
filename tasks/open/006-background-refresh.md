---
title: Keep the dashboard fresh without a page load
state: open
priority: low
labels: [control-plane, enhancement]
---

# Keep the dashboard fresh without a page load

## Context

Refresh is lazy: a fetch happens only when a page is loaded and the last
fetch is older than `max_age`. A dashboard left open on a second screen goes
stale, and the owner learns about a new question only by reloading. The
mirror layer already serialises fetches per repo, so a timer is cheap.

## Outcome

- A background thread refreshes every mirror on a configurable interval
  (`refresh_every`, default 120s), reusing the lazy path so a page load and
  the timer never fetch twice.
- The dashboard reloads itself when the data changed since it was rendered
  (a `<meta refresh>` or a tiny poll against a version endpoint — no build
  step either way).
- A repo that cannot be reached is retried on the same cadence and shown as
  unreachable, never dropped.
