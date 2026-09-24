"""What the pages ask for: the state of every repo, and the few writes allowed.

Reads go through the mirrors' checkouts; writes go through
:meth:`Mirror.commit_push`, each as one small commit on the repo's default
branch — the same metadata-only exception to "never commit to main" that a
`grind` claim uses.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..conversation import Entry, new_entry
from ..conversation import append as append_entry
from ..discovery import STATES, Task, is_tasks_dir, load_tasks
from ..mirror import DEFAULT_MAX_AGE, Mirror, MirrorError, RefreshResult
from ..queue_ops import QueueError, clear_next, enqueue, metadata_file, promote

_ACTIVE = ("open", "ongoing")

QUEUE_OPS = ("enqueue", "promote", "unqueue")


class ControlError(Exception):
    """A request that cannot be honoured, worded for the page."""


@dataclass
class RepoView:
    """One repository as the dashboard sees it."""

    mirror: Mirror
    tasks: list[Task] = field(default_factory=list)
    refresh: RefreshResult | None = None
    error: str = ""

    @property
    def name(self) -> str:
        return self.mirror.name

    @property
    def queue(self) -> list[Task]:
        return [t for t in self.tasks if t.next_rank is not None]

    @property
    def ongoing(self) -> list[Task]:
        return [t for t in self.tasks if t.state == "ongoing"]

    @property
    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for task in self.tasks:
            counts[task.state] += 1
        return counts


@dataclass(frozen=True)
class Waiting:
    """A task whose thread ends in a question nobody has answered."""

    repo: str
    task: Task
    question: Entry

    @property
    def on_owner(self) -> bool:
        return self.question.author != self._owner

    _owner: str = ""


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
        views = []
        for mirror in self._mirrors.values():
            view = RepoView(mirror, refresh=mirror.last_refresh, error=self._errors.get(mirror.name, ""))
            if is_tasks_dir(mirror.tasks_dir):
                view.tasks = load_tasks(mirror.tasks_dir, _ACTIVE)
            elif not view.error:
                view.error = "no tasks/ folder"
            views.append(view)
        return views

    def waiting(self, views: list[RepoView] | None = None) -> list[Waiting]:
        """Every open question across the repos, the oldest first."""
        found = []
        for view in views or self.overview():
            for task in view.tasks:
                question = task.open_question
                if question is not None:
                    found.append(Waiting(view.name, task, question, self.owner))
        found.sort(key=lambda w: (w.question.when is None, w.question.when or 0, w.task.sort_key))
        return found

    def repo(self, name: str) -> RepoView:
        mirror = self.mirror(name)
        view = RepoView(mirror, refresh=mirror.last_refresh, error=self._errors.get(name, ""))
        if is_tasks_dir(mirror.tasks_dir):
            view.tasks = load_tasks(mirror.tasks_dir)
        return view

    def task(self, name: str, task_id: str) -> Task:
        mirror = self.mirror(name)
        if is_tasks_dir(mirror.tasks_dir):
            for task in load_tasks(mirror.tasks_dir):
                if task.task_id == task_id:
                    return task
        raise ControlError(f"{name} has no task {task_id!r}")

    def reply(self, name: str, task_id: str, kind: str, text: str) -> Entry:
        """Append an entry to the task's thread and push it."""
        entry = new_entry(kind, self.owner, text)
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
            raise ControlError(f"unknown queue operation {op!r}")
        mirror = self.mirror(name)

        def edit(root: Path) -> list[Path]:
            tasks_dir = root / "tasks"
            task = self._find(root, task_id, name)
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
        except MirrorError as error:
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
