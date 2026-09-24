"""What the repository remembers about its tasks: the commits on ``tasks/``.

Every step of the grind loop is a small commit on the default branch with a
structured subject — ``task 053: claim``, ``task 053: plan``,
``task 053: closed`` — and the control plane's own writes follow the same
shape (``task 053: answer from erikarne``). GitHub's merge commits name the
branch they merged. So the git log *is* the activity log, and nothing has to
report anything for a director to see what happened.

Everything here is read-only and local: it reads the mirror's own history
and its remote-tracking branches as of the last fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .remote import FETCH_TIMEOUT, run_git

# One record per commit, fields split by unit separators. `%aI` is the
# strict ISO author date, which datetime.fromisoformat reads directly.
_FORMAT = "%x1e%H%x1f%an%x1f%aI%x1f%s"

_TASK_SUBJECT_RE = re.compile(r"^task\s+(\d+)\s*[:\-—]\s*(.*)$", re.IGNORECASE)
_TASK_PATH_RE = re.compile(r"^tasks/(?:open|ongoing|closed)/(\d+)-")
_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from \S+?/(\S+)")
# A squash merge keeps the PR title — grind titles PRs "task NNN: ..." — and
# GitHub appends the number.
_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
_BRANCH_NUMBER_RE = re.compile(r"task/(\d+)")

# Subject prefixes, in the order they are tried, and the verb they mean.
_VERBS = (
    ("claim", "claimed"),
    ("plan", "planned"),
    ("close", "closed"),
    ("reopen", "reopened"),
    ("releas", "released"),
    ("answer", "answer"),
    ("question", "question"),
    ("note", "note"),
    ("queued first", "queued first"),
    ("unqueue", "unqueued"),
    ("queue", "queued"),
    ("file", "filed"),
)


@dataclass(frozen=True)
class Commit:
    """One commit that touched a task, read from the log."""

    sha: str
    author: str
    when: datetime
    subject: str
    paths: tuple[str, ...] = ()

    @property
    def number(self) -> str | None:
        """The task number the commit is about, from its subject or its paths."""
        match = _TASK_SUBJECT_RE.match(self.subject)
        if match:
            return match.group(1)
        merge = _MERGE_RE.match(self.subject)
        if merge:
            branch = _BRANCH_NUMBER_RE.search(merge.group(2))
            return branch.group(1) if branch else None
        for path in self.paths:
            found = _TASK_PATH_RE.match(path)
            if found:
                return found.group(1)
        return None

    @property
    def verb(self) -> str:
        """What happened, in one word the pages can show."""
        if self.pull_request is not None:
            return "merged"
        match = _TASK_SUBJECT_RE.match(self.subject)
        if match:
            rest = match.group(2).lower()
            for prefix, verb in _VERBS:
                if rest.startswith(prefix):
                    return verb
        return "changed"

    @property
    def pull_request(self) -> int | None:
        merge = _MERGE_RE.match(self.subject) or _SQUASH_RE.search(self.subject)
        return int(merge.group(1)) if merge else None

    @property
    def about_tasks(self) -> bool:
        """Worth an event: it merged a pull request or it touched ``tasks/``."""
        return self.pull_request is not None or any(p.startswith("tasks/") for p in self.paths)

    @property
    def detail(self) -> str:
        """The subject with the ``task NNN:`` prefix removed."""
        match = _TASK_SUBJECT_RE.match(self.subject)
        return match.group(2) if match else self.subject


def task_commits(root: Path, since: datetime | None = None, limit: int = 400) -> list[Commit]:
    """Commits on the checked-out branch that touched ``tasks/``, newest first.

    Only the first-parent line: the metadata commits grind makes on main and
    the merges that brought pull requests in. What happened inside a PR is
    the PR's business. Merges count even when they changed nothing under
    ``tasks/``, so a pull request landing is one event. The log's own order
    is kept, which is what tells two commits in the same second apart.
    """
    args = ["log", "--first-parent", f"--format={_FORMAT}", "--name-only", f"-n{limit}"]
    if since is not None:
        args.append(f"--since={since.isoformat()}")
    result = run_git(root, *args, timeout=FETCH_TIMEOUT)
    if not result.ok:
        return []
    return [commit for commit in _parse(result.out) if commit.about_tasks]


def branch_tips(root: Path, prefix: str = "task/") -> dict[str, datetime]:
    """Every remote branch under ``prefix`` and when it was last committed to.

    A task's branch is the closest thing to a heartbeat the repository has:
    the agent works there, and each push moves the tip.
    """
    result = run_git(
        root,
        "for-each-ref",
        "--format=%(refname:short)%09%(committerdate:iso-strict)",
        f"refs/remotes/origin/{prefix}",
        timeout=FETCH_TIMEOUT,
    )
    tips: dict[str, datetime] = {}
    if not result.ok:
        return tips
    for line in result.out.splitlines():
        name, _, stamp = line.partition("\t")
        when = _when(stamp)
        if name.startswith("origin/") and when is not None:
            tips[name[len("origin/"):]] = when
    return tips


def branch_number(branch: str) -> str | None:
    """``053`` from ``task/053_batched_gpu`` or ``task/053-batched-gpu``."""
    match = _BRANCH_NUMBER_RE.search(branch)
    return match.group(1) if match else None


def _parse(text: str) -> list[Commit]:
    commits = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        header, _, rest = record.partition("\n")
        fields = header.split("\x1f")
        if len(fields) != 4:
            continue
        sha, author, stamp, subject = fields
        when = _when(stamp)
        if when is None:
            continue
        paths = tuple(line.strip() for line in rest.splitlines() if line.strip())
        commits.append(Commit(sha, author, when, subject, paths))
    return commits


def _when(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(stamp.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
