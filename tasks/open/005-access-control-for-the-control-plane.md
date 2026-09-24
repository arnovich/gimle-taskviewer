---
title: Access control so the control plane can leave localhost
state: open
priority: high
labels: [control-plane, security]
---

# Access control so the control plane can leave localhost

## Context

`tv-web` binds to `127.0.0.1` and has no authentication: anyone who can reach
it can push commits as the owner. That is fine on a laptop and unacceptable
anywhere else, and the point of a control plane is to be reachable from
anywhere. This must land before it is deployed and before task 002 lets it
start agents.

## Outcome

- A shared secret in `web.toml` (`token = "..."`) that, when set, every
  request must present — as a cookie set by a login page, so the browser is
  the client and no header juggling is needed.
- With no token configured the app refuses to bind to anything but
  localhost, and says why.
- Forms carry a CSRF token; a cross-site POST cannot append an entry or
  change the queue.
- The README says: put it behind TLS (a reverse proxy or a tunnel); the app
  does not terminate TLS itself.
