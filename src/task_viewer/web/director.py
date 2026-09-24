"""What a director of agents needs to see, derived from the repositories.

Nothing here talks to an agent. Every answer is read from task files, the
threads in them, the commits on ``tasks/`` and the task branches on the
remote — the only traces an agent leaves. That is the deal: agents are
legible through what they write, and this module is where those traces
become "whose move is it", "who is working on what", "what happens next"
and "what just happened".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..conversation import Entry
from ..discovery import Task
from ..github import Pull, Run, Snapshot
from ..history import Commit, branch_number

# A claim that has produced neither a commit nor a word for this long is
# probably a session that died; the owner should know.
STALE_AFTER = timedelta(hours=4)

# The dashboard's feed looks back this far.
FEED_WINDOW = timedelta(days=7)
FEED_LIMIT = 60

# A failed run on a task branch older than this is history, not something to
# act on. The default branch is different: it is failing until it passes.
CI_WINDOW = timedelta(hours=24)


@dataclass
class RepoFacts:
    """Everything the director derives from, for one repo."""

    name: str
    tasks: list[Task]
    commits: list[Commit] = field(default_factory=list)
    branch_tips: dict[str, datetime] = field(default_factory=dict)
    github: Snapshot | None = None

    def task_for_branch(self, branch: str) -> Task | None:
        """The task a ``task/NNN_...`` branch belongs to, by its number."""
        number = branch_number(branch)
        if number is None:
            return None
        for task in self.tasks:
            if task.number == number:
                return task
        return None

    def branch_for(self, task: Task) -> str | None:
        """The remote task branch for this task, by its number.

        The ``branch:`` field is removed when a task closes, and older repos
        spell the slug with dashes rather than underscores, so the number is
        the only reliable key.
        """
        branch = task.meta.get("branch")
        if isinstance(branch, str) and branch in self.branch_tips:
            return branch
        number = task.number
        if number is None:
            return None
        for name in self.branch_tips:
            if branch_number(name) == number:
                return name
        return None

    def duplicates(self) -> dict[str, list[Task]]:
        by_number: dict[str, list[Task]] = {}
        for task in self.tasks:
            if task.number is not None:
                by_number.setdefault(task.number, []).append(task)
        return {n: ts for n, ts in by_number.items() if len(ts) > 1}

    def is_closed(self, ref: str) -> bool | None:
        """Whether the task ``ref`` names (id or number) is closed; None if unknown."""
        number = ref.split("-", 1)[0]
        for task in self.tasks:
            if task.task_id == ref or task.number == number:
                return task.state == "closed"
        return None


@dataclass(frozen=True)
class Waiting:
    repo: str
    task: Task
    question: Entry

    @property
    def on_owner(self) -> bool:
        return self.question.by_agent


@dataclass(frozen=True)
class OpenPull:
    """A pull request waiting on you, with the task it came from when known."""

    repo: str
    pull: Pull
    task: Task | None


@dataclass(frozen=True)
class StrayBranch:
    """A closed task's branch still on the remote with no pull request.

    Not a decision, housekeeping: a merged branch nobody deleted, or work
    that was never proposed. Listed, never counted.
    """

    repo: str
    task: Task
    branch: str
    since: datetime


@dataclass(frozen=True)
class GaveUp:
    repo: str
    task: Task
    attempts: int


@dataclass(frozen=True)
class Ambiguous:
    repo: str
    number: str
    tasks: list[Task]


@dataclass
class Attention:
    """Everything that is the owner's move, most urgent kind first."""

    questions: list[Waiting] = field(default_factory=list)
    pulls: list[OpenPull] = field(default_factory=list)  # your move: ready for review
    theirs: list[OpenPull] = field(default_factory=list)  # drafts, changes requested: the agent's move
    gave_up: list[GaveUp] = field(default_factory=list)
    ambiguous: list[Ambiguous] = field(default_factory=list)
    asked: list[Waiting] = field(default_factory=list)  # the owner's own open questions
    strays: list[StrayBranch] = field(default_factory=list)  # listed, not counted
    unknown: list[str] = field(default_factory=list)  # repos GitHub could not be asked about

    def pull_for(self, repo: str, task: Task) -> OpenPull | None:
        for candidate in self.pulls + self.theirs:
            if candidate.repo == repo and candidate.task is not None and candidate.task.task_id == task.task_id:
                return candidate
        return None

    @property
    def count(self) -> int:
        return len(self.questions) + len(self.pulls) + len(self.gave_up) + len(self.ambiguous)

    @property
    def summary(self) -> str:
        """``2 questions, 1 branch ready to merge`` — what the number is made of."""
        parts = []
        if self.questions:
            parts.append(_plural(len(self.questions), "question"))
        if self.pulls:
            parts.append(_plural(len(self.pulls), "pull request") + " to review")
        if self.gave_up:
            parts.append(_plural(len(self.gave_up), "task") + " given up on")
        if self.ambiguous:
            parts.append(_plural(len(self.ambiguous), "ambiguous number"))
        return ", ".join(parts)


@dataclass
class Agent:
    """One handle and what it holds, with the last sign of life."""

    handle: str
    holds: list[tuple[str, Task]] = field(default_factory=list)
    since: datetime | None = None
    last_entry: Entry | None = None
    last_entry_repo: str = ""
    last_commit: datetime | None = None
    last_branch: str = ""

    @property
    def last_seen(self) -> datetime | None:
        candidates = [
            when for when in (self.since, self.last_commit, self.last_entry and self.last_entry.when)
            if when is not None
        ]
        return max(candidates) if candidates else None

    @property
    def stale(self) -> bool:
        seen = self.last_seen
        return seen is not None and datetime.now(timezone.utc) - seen > STALE_AFTER


@dataclass(frozen=True)
class Pick:
    """A ranked task and why grind would, or would not, take it."""

    task: Task
    reason: str  # empty when it is pickable


@dataclass(frozen=True)
class Event:
    """One thing that happened, for the feed and the task timeline."""

    when: datetime
    repo: str
    kind: str  # claimed, planned, closed, merged, question, answer, note, queued ...
    who: str
    text: str
    task_id: str | None = None
    task_title: str = ""
    pull_request: int | None = None
    source: str = "commit"  # or "entry": a thread entry rather than a commit

    @property
    def is_entry(self) -> bool:
        return self.source == "entry"


def next_pick(picks: list[Pick]) -> Pick | None:
    """The first ranked task nothing holds back, or ``None``."""
    return next((p for p in picks if not p.reason), None)


def attention(facts: list[RepoFacts]) -> Attention:
    found = Attention()
    for repo in facts:
        dupes = repo.duplicates()
        for number, tasks in sorted(dupes.items()):
            found.ambiguous.append(Ambiguous(repo.name, number, tasks))
        snapshot = repo.github
        if snapshot is not None and snapshot.pulls_ok:
            for pull in snapshot.pulls:
                item = OpenPull(repo.name, pull, repo.task_for_branch(pull.branch))
                # A draft, or one the reviewer sent back, is the agent's move.
                (found.theirs if pull.draft or pull.review == "CHANGES_REQUESTED" else found.pulls).append(item)
        elif snapshot is not None:
            found.unknown.append(repo.name)
        with_pull = {p.branch for p in snapshot.pulls} if snapshot is not None and snapshot.pulls_ok else None
        for task in repo.tasks:
            question = task.open_question
            if question is not None:
                target = found.questions if question.by_agent else found.asked
                target.append(Waiting(repo.name, task, question))
            if task.state == "closed":
                branch = repo.branch_for(task)
                # Only when GitHub answered: without it a branch could be a PR.
                if branch is not None and with_pull is not None and branch not in with_pull:
                    found.strays.append(StrayBranch(repo.name, task, branch, repo.branch_tips[branch]))
            elif task.attempts >= 2:
                found.gave_up.append(GaveUp(repo.name, task, task.attempts))
    oldest = lambda w: (w.question.when is None, w.question.when or 0, w.task.sort_key)
    found.questions.sort(key=oldest)
    found.asked.sort(key=oldest)
    found.pulls.sort(key=lambda p: (p.pull.updated is None, p.pull.updated or 0))
    found.theirs.sort(key=lambda p: (p.pull.updated is None, p.pull.updated or 0))
    found.strays.sort(key=lambda s: s.since)
    return found


@dataclass(frozen=True)
class LiveRun:
    """A run that is running or queued, or failed recently, with its task."""

    repo: str
    run: Run
    task: Task | None


@dataclass
class Ci:
    """What the workflows are doing right now, across the repos."""

    running: list[LiveRun] = field(default_factory=list)
    queued: list[LiveRun] = field(default_factory=list)
    failed: list[LiveRun] = field(default_factory=list)  # latest per workflow+branch, last 24h
    unknown: list[str] = field(default_factory=list)  # repos that could not be asked

    @property
    def quiet(self) -> bool:
        return not (self.running or self.queued or self.failed)

    @property
    def summary(self) -> str:
        parts = []
        if self.running:
            parts.append(_plural(len(self.running), "run") + " running")
        if self.queued:
            parts.append(_plural(len(self.queued), "run") + " queued")
        if self.failed:
            parts.append(_plural(len(self.failed), "failure"))
        return ", ".join(parts) if parts else "all quiet"


def ci(facts: list[RepoFacts], branches: dict[str, str | None] | None = None, now: datetime | None = None) -> Ci:
    """Running and queued runs, and the latest real failure per workflow and branch.

    "Latest" looks only at runs that decided something: a cancelled or
    skipped run after a failure does not clear it. A failure on a task
    branch older than ``CI_WINDOW`` is history; on the default branch (named
    per repo in ``branches``) it stays until a run passes, which is also
    what the sidebar dot says.
    """
    now = now or datetime.now(timezone.utc)
    branches = branches or {}
    found = Ci()
    for repo in facts:
        snapshot = repo.github
        if snapshot is None:
            continue
        if not snapshot.runs_ok:
            found.unknown.append(repo.name)
            continue
        default = branches.get(repo.name)
        for run in snapshot.runs:
            live = LiveRun(repo.name, run, repo.task_for_branch(run.branch))
            if run.state == "running":
                found.running.append(live)
            elif run.state == "queued":
                found.queued.append(live)
        for run in _latest_decided(snapshot.runs).values():
            if run.state != "failed" or run.when is None:
                continue
            if run.branch == default or now - run.when <= CI_WINDOW:
                found.failed.append(LiveRun(repo.name, run, repo.task_for_branch(run.branch)))
    newest = lambda r: -((r.run.when or now).timestamp())
    found.running.sort(key=newest)
    found.queued.sort(key=newest)
    found.failed.sort(key=newest)
    return found


def main_health(repo: RepoFacts, branch: str | None) -> str:
    """``passing``, ``failing``, ``running`` or ``""`` for the default branch's workflows.

    The same rule as :func:`ci`: the latest run per workflow that passed or
    failed decides; a failure outranks a run in progress, since the fix is
    not in until it passes.
    """
    snapshot = repo.github
    if snapshot is None or not snapshot.runs_ok or branch is None:
        return ""
    on_branch = [run for run in snapshot.runs if run.branch == branch]
    latest = _latest_decided(on_branch)
    if any(run.state == "failed" for run in latest.values()):
        return "failing"
    if any(run.live for run in on_branch):
        return "running"
    return "passing" if latest else ""


def _latest_decided(runs: list[Run]) -> dict[tuple[str, str], Run]:
    """The newest passed-or-failed run per (workflow, branch)."""
    latest: dict[tuple[str, str], Run] = {}
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for run in runs:
        if not run.decided:
            continue
        key = (run.workflow, run.branch)
        if key not in latest or (run.when or floor) > (latest[key].when or floor):
            latest[key] = run
    return latest


def agents(facts: list[RepoFacts]) -> list[Agent]:
    """Who holds what right now, with the last time each one was heard from."""
    by_handle: dict[str, Agent] = {}
    for repo in facts:
        for task in repo.tasks:
            if task.state != "ongoing":
                continue
            handle = str(task.meta.get("claimed_by") or "unknown")
            agent = by_handle.setdefault(handle, Agent(handle))
            agent.holds.append((repo.name, task))
            claimed = _stamp(task.meta.get("claimed_at"))
            if claimed is not None and (agent.since is None or claimed < agent.since):
                agent.since = claimed
            branch = repo.branch_for(task)
            if branch is not None:
                tip = repo.branch_tips[branch]
                if agent.last_commit is None or tip > agent.last_commit:
                    agent.last_commit, agent.last_branch = tip, branch
            for entry in task.conversation:
                if entry.author == handle and entry.when is not None:
                    if agent.last_entry is None or (agent.last_entry.when or entry.when) <= entry.when:
                        agent.last_entry, agent.last_entry_repo = entry, repo.name
    return sorted(by_handle.values(), key=lambda a: (a.last_seen is None, -(a.last_seen or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))


def up_next(repo: RepoFacts) -> list[Pick]:
    """The ranked queue as grind reads it: the first pick with no reason is next."""
    dupes = repo.duplicates()
    picks = []
    ranked = sorted(
        (t for t in repo.tasks if t.next_rank is not None and t.state != "closed"),
        key=lambda t: (t.next_rank or 0, t.sort_key),
    )
    for task in ranked:
        picks.append(Pick(task, _reason(task, repo, dupes)))
    return picks


def _reason(task: Task, repo: RepoFacts, dupes: dict[str, list[Task]]) -> str:
    if task.state == "ongoing":
        return f"claimed by {task.meta.get('claimed_by', 'someone')}"
    question = task.open_question
    if question is not None and question.by_agent:
        return "waiting on your answer"
    if task.attempts >= 2:
        return f"given up after {task.attempts} attempts"
    if task.number is not None and task.number in dupes:
        return f"number {task.number} is ambiguous"
    for ref in task.depends_on:
        closed = repo.is_closed(ref)
        if closed is False:
            return f"blocked by {ref}"
    return ""


def feed(
    facts: list[RepoFacts], now: datetime | None = None, limit: int = FEED_LIMIT
) -> list[Event]:
    """Everything that happened across the repos lately, newest first."""
    now = now or datetime.now(timezone.utc)
    since = now - FEED_WINDOW
    events: list[Event] = []
    for repo in facts:
        titles = {t.number: t for t in repo.tasks if t.number is not None}
        for commit in repo.commits:
            if commit.when < since:
                continue
            task = titles.get(commit.number or "")
            events.append(_commit_event(repo.name, commit, task))
        for task in repo.tasks:
            for entry in task.conversation:
                if entry.when is not None and entry.when >= since:
                    events.append(_entry_event(repo.name, task, entry))
    events.sort(key=lambda e: e.when, reverse=True)
    return _dedupe(events)[:limit]


def timeline(repo: RepoFacts, task: Task) -> list[Event]:
    """One task's history: its commits and its thread, oldest first."""
    events = [
        _commit_event(repo.name, c, task)
        for c in reversed(repo.commits)  # oldest first, so equal stamps keep log order
        if c.number is not None and c.number == task.number
    ]
    events += [_entry_event(repo.name, task, e) for e in task.conversation if e.when is not None]
    events.sort(key=lambda e: e.when)
    return _dedupe(events)


def _commit_event(repo: str, commit: Commit, task: Task | None) -> Event:
    """A commit as one line: what happened, to which task, and what it said.

    The message is the PR title for a merge and the subject otherwise, left
    out when it only repeats the verb. A merge of a branch that is not a
    task's is named by its branch, so the line still says what landed.
    """
    if task is not None:
        title = task.title
    elif commit.branch and commit.pull_request:
        title = commit.branch
    else:
        title = f"task {commit.number}" if commit.number else ""
    return Event(
        when=commit.when,
        repo=repo,
        kind=commit.verb,
        who=commit.author,
        text=commit.title if commit.says_more_than_its_verb else "",
        task_id=task.task_id if task else None,
        task_title=title,
        pull_request=commit.pull_request,
    )


def _entry_event(repo: str, task: Task, entry: Entry) -> Event:
    return Event(
        when=entry.when or datetime.min.replace(tzinfo=timezone.utc),
        repo=repo,
        kind=entry.kind,
        who=entry.author,
        text=entry.text,
        task_id=task.task_id,
        task_title=task.title,
        source="entry",
    )


def _dedupe(events: list[Event]) -> list[Event]:
    """A thread entry and the commit that pushed it are one event, not two.

    The control plane's own commits say ``answer from erikarne``; the entry
    says the same thing with the text. Keep the entry, drop the commit.
    """
    kept: list[Event] = []
    entry_keys = {(e.repo, e.task_id, e.kind) for e in events if e.is_entry}
    for event in events:
        if not event.is_entry and (event.repo, event.task_id, event.kind) in entry_keys:
            continue
        kept.append(event)
    return kept


# Sort orders the repo page offers, and the default direction of each.
SORTS = ("number", "title", "created", "priority", "state")
_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
_STATE_RANK = {"ongoing": 0, "open": 1, "closed": 2}


def created_at(repo: RepoFacts) -> dict[str, datetime]:
    """When each task first appeared, from the oldest commit that named it.

    Only as far back as the log was read; older tasks have no date and sort
    last. A ``created:`` in the frontmatter, when the author wrote one, wins.
    """
    first: dict[str, datetime] = {}
    for commit in reversed(repo.commits):  # oldest first
        number = commit.number
        if number is not None and number not in first:
            first[number] = commit.when
    found: dict[str, datetime] = {}
    for task in repo.tasks:
        written = _stamp(task.meta.get("created"))
        if written is not None:
            found[task.task_id] = written
        elif task.number is not None and task.number in first:
            found[task.task_id] = first[task.number]
    return found


def arrange(repo: RepoFacts, tasks: list[Task], sort: str, descending: bool, query: str) -> list[Task]:
    """The tasks a repo page lists: filtered by ``query``, ordered by ``sort``."""
    needle = query.strip().lower()
    if needle:
        tasks = [t for t in tasks if needle in _haystack(t)]
    if sort not in SORTS:
        sort = "number"
    floor = datetime.max.replace(tzinfo=timezone.utc)
    if sort == "title":
        key = lambda t: (t.title.lower(), t.sort_key)
    elif sort == "created":
        dates = created_at(repo)
        key = lambda t: (t.task_id not in dates, dates.get(t.task_id, floor), t.sort_key)
    elif sort == "priority":
        key = lambda t: (_PRIORITY_RANK.get((t.priority or "").lower(), 3), t.sort_key)
    elif sort == "state":
        key = lambda t: (_STATE_RANK.get(t.state, 3), t.sort_key)
    else:
        key = lambda t: t.sort_key
    ordered = sorted(tasks, key=key)
    if descending:
        ordered.reverse()
        if sort == "created":  # unknown dates stay last either way
            known = [t for t in ordered if t.task_id in created_at(repo)]
            ordered = known + [t for t in ordered if t not in known]
    return ordered


def _haystack(task: Task) -> str:
    return " ".join([task.task_id, task.title, " ".join(task.labels), task.body]).lower()


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def _stamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None
