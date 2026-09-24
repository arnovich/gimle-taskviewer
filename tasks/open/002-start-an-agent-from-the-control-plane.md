---
title: Start an agent from the control plane
state: open
priority: medium
labels: [control-plane, agents]
depends_on: [005-access-control-for-the-control-plane]
---

# Start an agent from the control plane

## Context

The whole design is that the owner never talks to an agent directly: an agent
is started with one instruction — *grind this repo* — and everything it does
comes back as commits and PRs. So starting one should be a button on the repo
page, not a terminal session. Where it runs (a local `claude -p`, a sandbox
service, a VPS) is a provider detail; the control plane only needs to hand it
a repo URL and the instruction, and never needs to hear from it again.

## Outcome

- A provider interface with one method, *start(repo_url, instruction)*, and
  one implementation that runs `claude -p "grind <repo>"` locally in a fresh
  clone, so the flow is testable end to end on one machine.
- A *Start an agent* action on the repo page that calls it. Nothing is
  recorded by the control plane: the agent's own claim commit is the record.
  The page says "started" and the next refresh shows the claim.
- The provider is chosen in `web.toml`; an unconfigured provider hides the
  button rather than failing on click.

## Notes

A remote provider (a sandbox or VPS with the `gimle-skills` plugin and git
credentials for the repo) is a second implementation of the same interface,
filed separately once the first one works.
