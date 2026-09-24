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
from ..history import Commit, branch_number

# A claim that has produced neither a commit nor a word for this long is
# probably a session that died; the owner should know.
STALE_AFTER = timedelta(hours=4)

# The dashboard's feed looks back this far.
FEED_WINDOW = timedelta(days=7)
FEED_LIMIT = 60


@dataclass
class RepoFacts:
    """Everything the director derives from, for one repo."""

    name: str
    tasks: list[Task]
    commits: list[Commit] = field(default_factory=list)
    branch_tips: dict[str, datetime] = field(default_factory=dict)

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
class Ready:
    """A closed task whose branch is still on the remote: a PR awaiting you."""

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
    ready: list[Ready] = field(default_factory=list)
    gave_up: list[GaveUp] = field(default_factory=list)
    ambiguous: list[Ambiguous] = field(default_factory=list)
    asked: list[Waiting] = field(default_factory=list)  # the owner's own open questions

    @property
    def count(self) -> int:
        return len(self.questions) + len(self.ready) + len(self.gave_up) + len(self.ambiguous)

    @property
    def summary(self) -> str:
        """``2 questions, 1 branch ready to merge`` — what the number is made of."""
        parts = []
        if self.questions:
            parts.append(_plural(len(self.questions), "question"))
        if self.ready:
            parts.append(_plural(len(self.ready), "branch", "branches") + " ready to merge")
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
        for task in repo.tasks:
            question = task.open_question
            if question is not None:
                target = found.questions if question.by_agent else found.asked
                target.append(Waiting(repo.name, task, question))
            if task.state == "closed":
                branch = repo.branch_for(task)
                if branch is not None:
                    found.ready.append(Ready(repo.name, task, branch, repo.branch_tips[branch]))
            elif task.attempts >= 2:
                found.gave_up.append(GaveUp(repo.name, task, task.attempts))
    oldest = lambda w: (w.question.when is None, w.question.when or 0, w.task.sort_key)
    found.questions.sort(key=oldest)
    found.asked.sort(key=oldest)
    found.ready.sort(key=lambda r: r.since)
    return found


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
    return Event(
        when=commit.when,
        repo=repo,
        kind=commit.verb,
        who=commit.author,
        text=commit.detail,
        task_id=task.task_id if task else None,
        task_title=task.title if task else (f"task {commit.number}" if commit.number else ""),
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
