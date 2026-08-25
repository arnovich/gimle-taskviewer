"""The pull request a worktree's branch has open, and the two things you do to it.

Each worktree in the workspace is one line of work, and under the `grind` loop
each becomes one pull request. This module answers "what is the state of that
PR" for a whole repository in a single `gh` call, and carries the two actions
worth taking from the task viewer: leaving a comment for the agent, and merging.

Reads are cheap and safe. `merge` is outward-facing and effectively
irreversible, so the UI confirms before calling it — this module does not.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# One call carries everything the list rows and the detail pane need.
_FIELDS = (
    "number,title,body,url,headRefName,isDraft,mergeable,state,"
    "reviewDecision,statusCheckRollup,comments,updatedAt"
)

# GitHub App accounts that comment on every PR. They are not review feedback,
# and they would otherwise bury the comment that is.
_BOT_SUFFIXES = ("[bot]", "-bot")
_BOT_LOGINS = frozenset(
    {"cloudflare-workers-and-pages", "github-actions", "codecov", "vercel"}
)

# A conclusion that is neither of these means the check went red.
_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

_TIMEOUT = 30.0


@dataclass
class Comment:
    author: str
    body: str

    @property
    def is_bot(self) -> bool:
        login = self.author.lower()
        return login in _BOT_LOGINS or login.endswith(_BOT_SUFFIXES)


@dataclass
class PullRequest:
    """One open pull request, as `gh` reports it."""

    number: int
    title: str
    body: str
    url: str
    branch: str
    draft: bool = False
    mergeable: str = ""
    state: str = "OPEN"
    review_decision: str = ""
    checks_passed: int = 0
    checks_total: int = 0
    checks_failing: bool = False
    checks_pending: int = 0
    comments: list[Comment] = field(default_factory=list)

    @property
    def human_comments(self) -> list[Comment]:
        return [c for c in self.comments if not c.is_bot]

    @property
    def blocked(self) -> str | None:
        """Why merging now would be a bad idea, if it would be."""
        if self.draft:
            return "still a draft"
        if self.mergeable == "CONFLICTING":
            return "has conflicts with main"
        if self.checks_failing:
            failed = self.checks_total - self.checks_passed
            return f"{failed} check{'' if failed == 1 else 's'} failing"
        return None


@dataclass
class ActionResult:
    ok: bool
    message: str


def load_pull_requests(repo_root: Path) -> dict[str, PullRequest]:
    """Open pull requests for ``repo_root``, keyed by their head branch.

    Keyed by branch because that is what a worktree knows about itself.
    """
    out = _gh(repo_root, "pr", "list", "--json", _FIELDS, "--limit", "100")
    if out is None:
        return {}
    try:
        raw = json.loads(out)
    except ValueError:
        return {}
    found: dict[str, PullRequest] = {}
    for entry in raw:
        pull = _build(entry)
        if pull.branch:
            found[pull.branch] = pull
    return found


def _build(entry: dict) -> PullRequest:
    checks = entry.get("statusCheckRollup") or []
    conclusions = [(check.get("conclusion") or "").upper() for check in checks]
    passed = sum(1 for c in conclusions if c in _CHECK_OK)
    pending = sum(1 for c in conclusions if not c)
    return PullRequest(
        number=entry.get("number", 0),
        title=entry.get("title", ""),
        body=entry.get("body") or "",
        url=entry.get("url", ""),
        branch=entry.get("headRefName", ""),
        draft=bool(entry.get("isDraft")),
        mergeable=entry.get("mergeable") or "",
        state=entry.get("state") or "OPEN",
        review_decision=entry.get("reviewDecision") or "",
        checks_passed=passed,
        checks_total=len(checks),
        # Pending is not failing: a run still in flight has no conclusion yet.
        checks_failing=any(c and c not in _CHECK_OK for c in conclusions),
        checks_pending=pending,
        comments=[
            Comment((c.get("author") or {}).get("login", "?"), c.get("body") or "")
            for c in entry.get("comments") or []
        ],
    )


def checks_summary(pull: PullRequest) -> str:
    """``2/2 passing``, ``1 failing``, ``no checks`` — for a row or a pane."""
    if not pull.checks_total:
        return "no checks"
    if pull.checks_failing:
        return f"{pull.checks_total - pull.checks_passed} failing"
    if pull.checks_pending:
        return f"{pull.checks_pending} running"
    return f"{pull.checks_passed}/{pull.checks_total} passing"


def merge(repo_root: Path, number: int, admin: bool = False) -> ActionResult:
    """Merge a pull request with a merge commit.

    A merge commit because that is what this workspace does — every one of the
    last thirty landed that way. ``admin`` bypasses branch protection; it is a
    no-op where `main` is unprotected, and stays opt-in either way.
    """
    args = ["pr", "merge", str(number), "--merge"]
    if admin:
        args.append("--admin")
    result = _gh_result(repo_root, *args)
    if result.returncode != 0:
        return ActionResult(False, _first_line(result.stderr) or "gh refused the merge")
    return ActionResult(True, f"merged #{number}")


def comment(repo_root: Path, number: int, body: str) -> ActionResult:
    """Post a comment on a pull request."""
    if not body.strip():
        return ActionResult(False, "empty comment — nothing posted")
    result = _gh_result(repo_root, "pr", "comment", str(number), "--body", body)
    if result.returncode != 0:
        return ActionResult(False, _first_line(result.stderr) or "gh refused the comment")
    return ActionResult(True, f"commented on #{number}")


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _gh(repo_root: Path, *args: str) -> str | None:
    result = _gh_result(repo_root, *args)
    return result.stdout if result.returncode == 0 else None


def _gh_result(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run `gh` in ``repo_root``; never prompts, never blocks forever."""
    # Naming the repo explicitly makes a worktree resolve the same as its
    # main checkout, which `gh` alone does not always manage.
    slug = _slug(repo_root)
    command = ["gh", *args, "--repo", slug] if slug else ["gh", *args]
    try:
        return subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env=_env(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))


def _slug(repo_root: Path) -> str:
    """``owner/name`` for the repo, so a worktree resolves the same as its repo."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    if "github.com" not in url:
        return ""
    slug = url.split("github.com", 1)[1].lstrip(":/")
    return slug[:-4] if slug.endswith(".git") else slug


def _env() -> dict[str, str]:
    """gh must never stop to ask a question inside a full-screen TUI."""
    return {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
