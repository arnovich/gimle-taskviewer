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
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

# One call carries everything the list rows and the detail pane need.
_FIELDS = (
    "number,title,body,url,headRefName,isDraft,mergeable,reviewDecision,"
    "statusCheckRollup,comments,updatedAt,additions,deletions,changedFiles"
)

# GitHub App accounts that comment on every PR. They are not review feedback,
# and they would otherwise bury the comment that is.
_BOT_SUFFIXES = ("[bot]", "-bot")
_BOT_LOGINS = frozenset(
    {"cloudflare-workers-and-pages", "github-actions", "codecov", "vercel"}
)

# A conclusion that is neither of these means the check went red.
_CHECK_OK = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

# Our own marker, so a timeout is never reported as "gh refused" — the request
# may well have landed.
_TIMED_OUT = -9

_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()

_TIMEOUT = 30.0

# Tried in order; the first one present wins.
_OPENERS = {
    "linux": ("xdg-open", "gio", "wslview"),
    "darwin": ("open",),
    "win32": ("start",),
}

# owner/name, and nothing else. Anything with a leading dash or an extra path
# segment is not a repository we should be aiming a merge at.
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass
class Check:
    """One CI check. The name matters: "1 failing" does not say which."""

    name: str
    conclusion: str

    @property
    def failed(self) -> bool:
        return bool(self.conclusion) and self.conclusion not in _CHECK_OK

    @property
    def pending(self) -> bool:
        return not self.conclusion


@dataclass
class Comment:
    author: str
    body: str
    created_at: str = ""

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
    review_decision: str = ""
    checks: list[Check] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    updated_at: str = ""

    @property
    def checks_total(self) -> int:
        return len(self.checks)

    @property
    def checks_failed(self) -> list[Check]:
        return [check for check in self.checks if check.failed]

    @property
    def checks_pending(self) -> int:
        return sum(1 for check in self.checks if check.pending)

    @property
    def checks_passed(self) -> int:
        return sum(1 for c in self.checks if not c.failed and not c.pending)

    @property
    def checks_failing(self) -> bool:
        return bool(self.checks_failed)

    @property
    def diffstat(self) -> str:
        return f"{_count(self.changed_files, 'file')} · +{self.additions} −{self.deletions}"

    @property
    def human_comments(self) -> list[Comment]:
        return [c for c in self.comments if not c.is_bot]

    @property
    def blocking(self) -> str | None:
        """Why GitHub would refuse this merge outright.

        Distinct from :attr:`warnings`. A conflicted branch cannot be merged by
        anyone — ``--admin`` bypasses branch protection, not a three-way merge —
        so offering a merge key here offers a dead end.
        """
        if self.mergeable == "CONFLICTING":
            return "conflicts with main — needs a rebase"
        return None

    @property
    def warnings(self) -> list[str]:
        """Reasons to think twice, none of which prevent a merge."""
        notes: list[str] = []
        if self.draft:
            notes.append("still a draft")
        failed = self.checks_failed
        if failed:
            names = ", ".join(check.name for check in failed[:3])
            notes.append(f"{_count(len(failed), 'check')} failing ({names})")
        if self.mergeable == "UNKNOWN":
            # GitHub reports this for a while after a push. It is not "fine".
            notes.append("mergeability not yet known")
        return notes


@dataclass
class ActionResult:
    ok: bool
    message: str


@dataclass
class Listing:
    """Open PRs for one repo, and whether we actually managed to ask."""

    by_branch: dict[str, PullRequest] = field(default_factory=dict)
    reached: bool = True
    error: str = ""


def load_pull_requests(repo_root: Path) -> Listing:
    """Open pull requests for ``repo_root``, keyed by their head branch.

    Keyed by branch because that is what a worktree knows about itself. The
    ``reached`` flag matters: for a view whose job is "what is waiting on me",
    rendering nothing because the token expired looks exactly like a clear
    queue, which is the worst way to be wrong.
    """
    result = _gh_result(repo_root, "pr", "list", "--json", _FIELDS, "--limit", "100")
    if result.returncode != 0:
        return Listing(reached=False, error=_first_line(result.stderr) or "gh failed")
    try:
        raw = json.loads(result.stdout)
    except ValueError:
        return Listing(reached=False, error="gh returned something that is not JSON")
    if not isinstance(raw, list):
        # An error object on a zero exit — a wrapper or proxy is enough.
        return Listing(reached=False, error="gh returned an unexpected shape")
    found: dict[str, PullRequest] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pull = _build(entry)
        if pull.branch:
            found[pull.branch] = pull
    return Listing(found)


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"


def _build(entry: dict) -> PullRequest:
    return PullRequest(
        number=entry.get("number", 0),
        title=entry.get("title", ""),
        body=entry.get("body") or "",
        url=entry.get("url", ""),
        branch=entry.get("headRefName", ""),
        draft=bool(entry.get("isDraft")),
        mergeable=entry.get("mergeable") or "",
        review_decision=entry.get("reviewDecision") or "",
        # Pending is not failing: a run still in flight has no conclusion yet.
        checks=[
            Check(c.get("name") or "?", (c.get("conclusion") or "").upper())
            for c in entry.get("statusCheckRollup") or []
        ],
        comments=[
            Comment(
                (c.get("author") or {}).get("login", "?"),
                c.get("body") or "",
                c.get("createdAt") or "",
            )
            for c in entry.get("comments") or []
        ],
        additions=entry.get("additions") or 0,
        deletions=entry.get("deletions") or 0,
        changed_files=entry.get("changedFiles") or 0,
        updated_at=entry.get("updatedAt") or "",
    )


def checks_summary(pull: PullRequest) -> str:
    """``2/2 passing``, ``test failing`` — names what went red, not just how many."""
    if not pull.checks_total:
        return "no checks"
    failed = pull.checks_failed
    if failed:
        names = ", ".join(check.name for check in failed[:2])
        extra = f" +{len(failed) - 2}" if len(failed) > 2 else ""
        return f"{names}{extra} failing"
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
    if result.returncode == _TIMED_OUT:
        return ActionResult(False, f"timed out — check #{number} on GitHub before retrying")
    if result.returncode != 0:
        return ActionResult(False, _first_line(result.stderr) or "gh refused the merge")
    return ActionResult(True, f"merged #{number}")


def comment(repo_root: Path, number: int, body: str) -> ActionResult:
    """Post a comment on a pull request."""
    if not body.strip():
        return ActionResult(False, "empty comment — nothing posted")
    result = _gh_result(repo_root, "pr", "comment", str(number), "--body", body)
    if result.returncode == _TIMED_OUT:
        return ActionResult(False, f"timed out — check #{number} on GitHub")
    if result.returncode != 0:
        return ActionResult(False, _first_line(result.stderr) or "gh refused the comment")
    return ActionResult(True, f"commented on #{number}")


def open_in_browser(url: str) -> ActionResult:
    """Hand ``url`` to the desktop's URL opener.

    Detached and with its streams closed: a console browser inheriting the
    TUI's terminal would take it over, and a GUI one writing to stderr would
    scribble over the interface.
    """
    if not url.lower().startswith(("http://", "https://")):
        # The URL arrives from an API response, so check it is plainly a web
        # page before handing it to whatever the desktop has registered.
        return ActionResult(False, "not a web address — nothing opened")
    opener = _opener()
    if not opener:
        return ActionResult(False, f"no way to open a browser here — {url}")
    try:
        subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        return ActionResult(False, f"could not open a browser: {error}")
    return ActionResult(True, f"opened {url}")


def _opener() -> str:
    """The command this platform uses to open a URL, if it has one."""
    for candidate in _OPENERS.get(sys.platform, _OPENERS["linux"]):
        if shutil.which(candidate):
            return candidate
    return ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _gh_result(
    repo_root: Path, *args: str, _with_repo: bool = True
) -> subprocess.CompletedProcess:
    """Run `gh` in ``repo_root``; never prompts, never blocks forever."""
    # Naming the repo explicitly makes a worktree resolve the same as its main
    # checkout, which gh alone does not always manage.
    slug = _slug(repo_root) if _with_repo else ""
    command = ["gh", *args, "--repo", slug] if slug else ["gh", *args]
    try:
        child = subprocess.Popen(
            command,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="surrogateescape",
            env=_env(),
        )
    except (OSError, ValueError) as error:
        return subprocess.CompletedProcess(command, 1, "", str(error))

    # Registered so a quitting app can kill it: `q` must not wait on the network.
    with _running_lock:
        _running.add(child)
    try:
        out, err = child.communicate(timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.communicate()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return subprocess.CompletedProcess(command, _TIMED_OUT, "", "timed out")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        child.kill()
        return subprocess.CompletedProcess(command, 1, "", str(error))
    finally:
        with _running_lock:
            _running.discard(child)
    return subprocess.CompletedProcess(command, child.returncode, out or "", err or "")


def cancel_all() -> None:
    """Kill any in-flight gh child, so quitting never waits on the network."""
    with _running_lock:
        children = list(_running)
    for child in children:
        try:
            child.kill()
        except OSError:
            pass


def _slug(repo_root: Path) -> str:
    """``owner/name`` for the repo, asked of gh rather than guessed.

    Parsing the remote URL by hand looks easy and is not: a mirror path
    containing ``github.com/owner/name`` resolves to a real, unrelated GitHub
    repository, and ``gh pr merge --repo`` would then act on someone else's
    pull request number. gh already knows the answer.
    """
    result = _gh_result(repo_root, "repo", "view", "--json", "nameWithOwner",
                        "-q", ".nameWithOwner", _with_repo=False)
    if result.returncode != 0:
        return ""
    slug = result.stdout.strip()
    return slug if _SLUG_RE.fullmatch(slug) else ""


def _env() -> dict[str, str]:
    """gh must never stop to ask a question inside a full-screen TUI."""
    return {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}
