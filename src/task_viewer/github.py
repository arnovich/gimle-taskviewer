"""What GitHub knows about a repository: its workflow runs and open pull requests.

Everything comes through the ``gh`` CLI, with the login the machine already
has, so nothing here holds a token. Calls never prompt and always time out.
A repository that is not on GitHub, or a ``gh`` that is missing or logged
out, yields a :class:`Snapshot` with an ``error`` and empty lists — the
pages say so, and everything read from git still works.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

_TIMEOUT = 20.0
_RUNS = 30  # across all branches: what is running, queued, or just failed
_MAIN_RUNS = 20  # on the default branch alone: enough to judge its health
_PULLS = 50

# Variables that would send gh somewhere the config never named, or make it
# chatty on stderr, which ends up in a tooltip.
_UNWANTED_ENV = ("GH_HOST", "GH_REPO", "GH_ENTERPRISE_TOKEN", "GH_DEBUG", "DEBUG")

_SLUG_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?$"
)

_RUN_FIELDS = "databaseId,workflowName,displayTitle,status,conclusion,headBranch,event,createdAt,updatedAt,url"
_PULL_FIELDS = "number,title,headRefName,url,isDraft,createdAt,updatedAt,author,reviewDecision,mergeable,statusCheckRollup"

# gh's own vocabulary, folded to five words the pages can colour. A run
# awaiting approval (action_required) is neither a pass nor a failure.
_QUEUED = frozenset({"queued", "waiting", "pending", "requested"})
_FAILED = frozenset({"failure", "timed_out", "startup_failure"})
_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED", "STALE"})
_CHECK_BAD = frozenset({"FAILURE", "TIMED_OUT", "ERROR", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"})


@dataclass(frozen=True)
class Run:
    """One workflow run."""

    id: int
    workflow: str
    title: str
    status: str
    conclusion: str
    branch: str
    event: str
    created: datetime | None
    updated: datetime | None
    url: str

    @property
    def state(self) -> str:
        """``running``, ``queued``, ``passed``, ``failed``, ``cancelled`` or ``other``."""
        if self.status == "in_progress":
            return "running"
        if self.status in _QUEUED:
            return "queued"
        if self.status == "completed":
            if self.conclusion == "success":
                return "passed"
            if self.conclusion in _FAILED:
                return "failed"
            if self.conclusion == "cancelled":
                return "cancelled"
        return "other"

    @property
    def live(self) -> bool:
        return self.state in ("running", "queued")

    @property
    def decided(self) -> bool:
        """A real result — passed or failed. Cancelled and skipped decide nothing."""
        return self.state in ("passed", "failed")

    @property
    def when(self) -> datetime | None:
        return self.updated or self.created


@dataclass(frozen=True)
class Pull:
    """One open pull request, with its checks folded to one word."""

    number: int
    title: str
    branch: str
    url: str
    draft: bool
    author: str
    review: str  # APPROVED, CHANGES_REQUESTED, REVIEW_REQUIRED or ""
    mergeable: str  # MERGEABLE, CONFLICTING, UNKNOWN or ""
    checks: str  # passing, failing, pending or none
    checks_done: int
    checks_total: int
    created: datetime | None
    updated: datetime | None


@dataclass
class Snapshot:
    """What was known at ``checked``, and what could not be asked.

    Runs and pull requests are asked for separately and kept separately, so
    a repository with Actions disabled still shows its pull requests.
    """

    runs: list[Run] = field(default_factory=list)
    pulls: list[Pull] = field(default_factory=list)
    checked: datetime | None = None
    runs_error: str = ""
    pulls_error: str = ""

    @property
    def runs_ok(self) -> bool:
        return not self.runs_error

    @property
    def pulls_ok(self) -> bool:
        return not self.pulls_error

    @property
    def ok(self) -> bool:
        return self.runs_ok and self.pulls_ok

    @property
    def error(self) -> str:
        parts = []
        if self.runs_error:
            parts.append(f"runs: {self.runs_error}")
        if self.pulls_error:
            parts.append(f"pull requests: {self.pulls_error}")
        return "; ".join(parts)


def slug_of(url: str) -> str | None:
    """``owner/name`` when ``url`` is a GitHub remote, else ``None``."""
    match = _SLUG_RE.match(url.strip())
    return match.group(1) if match else None


def fetch(slug: str, default_branch: str | None = None) -> Snapshot:
    """Runs and open PRs for ``owner/name``, as of now. Never raises.

    The host is pinned to github.com, where the slug came from, so a
    ``GH_HOST`` in the owner's shell cannot redirect the question. Runs are
    asked for twice: the latest across all branches, and the latest on the
    default branch alone, so its health is judged on its own runs even when
    a busy repo's task branches crowd it out of the first list.
    """
    snapshot = Snapshot(checked=datetime.now(timezone.utc))
    repo = f"github.com/{slug}"
    try:
        runs, error = _gh_json("run", "list", "--repo", repo, "--limit", str(_RUNS), "--json", _RUN_FIELDS)
        if not error and default_branch:
            main_runs, error = _gh_json(
                "run", "list", "--repo", repo, "--branch", default_branch,
                "--limit", str(_MAIN_RUNS), "--json", _RUN_FIELDS,
            )
            runs = runs + main_runs
        if error:
            snapshot.runs_error = error
        else:
            seen: set[int] = set()
            for item in runs:
                if isinstance(item, dict):
                    run = _run(item)
                    if run.id not in seen:
                        seen.add(run.id)
                        snapshot.runs.append(run)
        pulls, error = _gh_json(
            "pr", "list", "--repo", repo, "--state", "open", "--limit", str(_PULLS), "--json", _PULL_FIELDS,
        )
        if error:
            snapshot.pulls_error = error
        else:
            snapshot.pulls = [_pull(item) for item in pulls if isinstance(item, dict)]
    except Exception as unexpected:  # noqa: BLE001 - a gh answer in a new shape must not 500 every page
        message = f"gh returned something unexpected: {unexpected}"
        snapshot.runs_error = snapshot.runs_error or message
        snapshot.pulls_error = snapshot.pulls_error or message
    return snapshot


def _gh_json(*args: str) -> tuple[list, str]:
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            stdin=subprocess.DEVNULL,
            env={
                **{k: v for k, v in os.environ.items() if k not in _UNWANTED_ENV},
                "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1",
                "GH_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
    except FileNotFoundError:
        return [], "gh is not installed"
    except subprocess.TimeoutExpired:
        return [], "gh timed out"
    except (OSError, ValueError) as error:
        return [], f"could not run gh: {error}"
    if proc.returncode != 0:
        return [], _first_line(proc.stderr) or "gh failed"
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return [], "gh returned something that is not JSON"
    return (data if isinstance(data, list) else []), ""


def _run(item: dict) -> Run:
    return Run(
        id=_int(item.get("databaseId")),
        workflow=str(item.get("workflowName") or ""),
        title=str(item.get("displayTitle") or ""),
        status=str(item.get("status") or ""),
        conclusion=str(item.get("conclusion") or ""),
        branch=str(item.get("headBranch") or ""),
        event=str(item.get("event") or ""),
        created=_when(item.get("createdAt")),
        updated=_when(item.get("updatedAt")),
        url=_https(item.get("url")),
    )


def _pull(item: dict) -> Pull:
    rollup = item.get("statusCheckRollup") or []
    states = [str(c.get("conclusion") or c.get("state") or "").upper() for c in rollup if isinstance(c, dict)]
    done = sum(1 for s in states if s in _CHECK_OK or s in _CHECK_BAD)
    if not states:
        checks = "none"
    elif any(s in _CHECK_BAD for s in states):
        checks = "failing"
    elif done < len(states):
        checks = "pending"
    else:
        checks = "passing"
    author = item.get("author") or {}
    return Pull(
        number=_int(item.get("number")),
        title=str(item.get("title") or ""),
        branch=str(item.get("headRefName") or ""),
        url=_https(item.get("url")),
        draft=bool(item.get("isDraft")),
        author=str(author.get("login") or "") if isinstance(author, dict) else "",
        review=str(item.get("reviewDecision") or ""),
        mergeable=str(item.get("mergeable") or ""),
        checks=checks,
        checks_done=done,
        checks_total=len(states),
        created=_when(item.get("createdAt")),
        updated=_when(item.get("updatedAt")),
    )


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _https(value: object) -> str:
    """A link the page may follow: https only, or nothing."""
    text = str(value or "")
    return text if text.startswith("https://") else ""


def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""
