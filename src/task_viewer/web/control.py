"""What the pages ask for: the state of every repo, and the few writes allowed.

Reads go through the mirrors' checkouts; writes go through
:meth:`Mirror.commit_push`, each as one small commit on the repo's default
branch — the same metadata-only exception to "never commit to main" that a
`grind` claim uses.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..conversation import ConversationError, Entry, new_entry
from ..conversation import append as append_entry
from ..discovery import STATES, Task, is_tasks_dir, load_tasks
from ..history import Commit, branch_tips, task_commits
from ..mirror import DEFAULT_MAX_AGE, Mirror, MirrorError, RefreshResult
from ..queue_ops import QueueError, clear_next, enqueue, metadata_file, promote
from ..textfile import TextFileError
from .director import RepoFacts

_ACTIVE = ("open", "ongoing")

QUEUE_OPS = ("enqueue", "promote", "unqueue")


class ControlError(Exception):
    """A request that cannot be honoured, worded for the page."""


class InvalidInput(ControlError):
    """The request itself is wrong, before anything is written."""


@dataclass
class RepoView:
    """One repository as the dashboard sees it."""

    mirror: Mirror
    tasks: list[Task] = field(default_factory=list)
    refresh: RefreshResult | None = None
    error: str = ""
    commits: list[Commit] = field(default_factory=list)
    branch_tips: dict[str, datetime] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.mirror.name

    @property
    def facts(self) -> RepoFacts:
        return RepoFacts(self.name, self.tasks, self.commits, self.branch_tips)

    @property
    def active(self) -> list[Task]:
        return [t for t in self.tasks if t.state != "closed"]

    @property
    def queue(self) -> list[Task]:
        return [t for t in self.active if t.next_rank is not None]

    @property
    def ongoing(self) -> list[Task]:
        return [t for t in self.tasks if t.state == "ongoing"]

    @property
    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for task in self.tasks:
            counts[task.state] += 1
        return counts


class ControlPlane:
    """The mirrors, plus the owner's identity for what they write."""

    def __init__(
        self, mirrors: list[Mirror], owner: str, max_age: float = DEFAULT_MAX_AGE
    ) -> None:
        self.owner = owner
        self.max_age = max_age
        self._mirrors = {mirror.name: mirror for mirror in mirrors}
        self._errors: dict[str, str] = {}

    @property
    def mirrors(self) -> list[Mirror]:
        return list(self._mirrors.values())

    def mirror(self, name: str) -> Mirror:
        try:
            return self._mirrors[name]
        except KeyError:
            raise ControlError(f"no repository called {name!r}") from None

    def refresh(self, force: bool = False) -> None:
        """Bring every mirror up to date, in parallel — one fetch per repo."""
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(self._mirrors)))) as pool:
            futures = {
                pool.submit(mirror.refresh, self.max_age, force): mirror
                for mirror in self._mirrors.values()
            }
            for future, mirror in futures.items():
                try:
                    future.result()
                    self._errors.pop(mirror.name, None)
                except MirrorError as error:
                    self._errors[mirror.name] = str(error)

    def overview(self) -> list[RepoView]:
        """Every repo with all of its tasks, read while the checkout holds still."""
        return [self._view(mirror) for mirror in self._mirrors.values()]

    def repo(self, name: str) -> RepoView:
        return self._view(self.mirror(name))

    def task(self, name: str, task_id: str) -> Task:
        for task in self.repo(name).tasks:
            if task.task_id == task_id:
                return task
        raise ControlError(f"{name} has no task {task_id!r}")

    def _view(self, mirror: Mirror) -> RepoView:
        view = RepoView(
            mirror, refresh=mirror.last_refresh, error=self._errors.get(mirror.name, "")
        )
        with mirror.locked():
            if is_tasks_dir(mirror.tasks_dir):
                view.tasks = load_tasks(mirror.tasks_dir)
            elif not view.error:
                view.error = "no tasks/ folder"
            if (mirror.root / ".git").exists():
                view.commits = task_commits(mirror.root)
                view.branch_tips = branch_tips(mirror.root)
        return view

    def reply(self, name: str, task_id: str, kind: str, text: str) -> Entry:
        """Append an entry to the task's thread and push it."""
        try:
            entry = new_entry(kind, self.owner, text)
        except ConversationError as error:
            raise InvalidInput(str(error)) from error
        mirror = self.mirror(name)

        def edit(root: Path) -> list[Path]:
            task = self._find(root, task_id, name)
            md_file = metadata_file(task)
            if md_file is None:
                raise ControlError(f"{task_id} has no markdown file to write to")
            append_entry(md_file, entry)
            return [md_file]

        label = f"{kind} from {self.owner}"
        self._push(mirror, edit, f"task {_number(task_id)}: {label}")
        return entry

    def queue(self, name: str, task_id: str, op: str) -> None:
        """Change the task's ``next:`` rank — the one field only the owner writes."""
        if op not in QUEUE_OPS:
            raise InvalidInput(f"unknown queue operation {op!r}")
        mirror = self.mirror(name)

        def edit(root: Path) -> list[Path]:
            tasks_dir = root / "tasks"
            task = self._find(root, task_id, name)
            if task.state == "closed":
                raise ControlError(f"{task_id} is closed; a closed task has no place in the queue")
            before = {t.path: t.next_rank for t in load_tasks(tasks_dir, _ACTIVE)}
            try:
                if op == "enqueue":
                    enqueue(task, tasks_dir)
                elif op == "promote":
                    promote(task, tasks_dir)
                else:
                    clear_next(task, tasks_dir)
            except QueueError as error:
                raise ControlError(str(error)) from error
            after = {t.path: t.next_rank for t in load_tasks(tasks_dir, _ACTIVE)}
            return [path for path, rank in after.items() if before.get(path) != rank]

        verbs = {"enqueue": "queued", "promote": "queued first", "unqueue": "unqueued"}
        self._push(mirror, edit, f"task {_number(task_id)}: {verbs[op]}")

    def _push(self, mirror: Mirror, edit, message: str) -> None:
        try:
            mirror.commit_push(edit, message)
        except (MirrorError, TextFileError, QueueError, ConversationError) as error:
            raise ControlError(str(error)) from error

    @staticmethod
    def _find(root: Path, task_id: str, name: str) -> Task:
        tasks_dir = root / "tasks"
        if is_tasks_dir(tasks_dir):
            for task in load_tasks(tasks_dir):
                if task.task_id == task_id:
                    return task
        raise ControlError(f"{name} has no task {task_id!r} any more")


def _number(task_id: str) -> str:
    head = task_id.split("-", 1)[0]
    return head if head.isdigit() else task_id
