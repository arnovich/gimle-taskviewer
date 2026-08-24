"""The Textual TUI.

Two navigation levels:

* **projects** — when ``tv`` is run from a workspace root, the left pane lists
  the child projects that have a tasks folder. ``→``/Enter steps into one.
* **tasks** — the task list on the left, rendered markdown on the right.
  ``←`` steps back to the project list (in workspace mode).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Label, ListItem, ListView, Markdown

from .discovery import STATES, Task, count_states, load_tasks
from .git_info import GitInfo, describe_age, format_moment, load_git_info
from .groom import GroomResult, run_groom
from .state_ops import StateChangeError, set_state
from .workspace import Project

_PRIORITY_STYLE = {
    "high": "bold red",
    "medium": "yellow",
    "low": "dim",
}

# Active states are shown by default; closed is folded in with the `o` toggle.
_ACTIVE_STATES = ("open", "ongoing")

_STATE_MARK = {"open": "○", "ongoing": "◐", "closed": "●"}

# Task-level keys are hidden while browsing the project list.
_TASK_ACTIONS = frozenset(
    {
        "work_on_task",
        "groom",
        "mark_ongoing",
        "mark_done",
        "mark_open",
        "toggle_closed",
        "reload",
    }
)

_EMPTY_BODY = "*Select a task on the left. Press `Tab` to move between panes.*"

# Branch names are long ("feat/strict_rewrite_proof_kernel") and the list pane is
# narrow, so rows show a truncated form and the summary pane the full one.
_ROW_BRANCH_WIDTH = 20


class TaskListView(ListView):
    """Left pane. Nothing extra yet, but a named subclass keeps CSS targeted."""


class MarkdownPane(VerticalScroll):
    """Right pane. Focusable so `Tab` reaches it and arrows scroll it."""

    can_focus = True


class TaskViewerApp(App):
    """Browse the markdown tasks of a project, or a workspace of projects."""

    CSS = """
    Screen {
        layout: horizontal;
    }

    TaskListView {
        width: 40%;
        max-width: 60;
        border: round $panel;
        padding: 0 1;
    }

    MarkdownPane {
        width: 1fr;
        border: round $panel;
        padding: 0 1;
    }

    TaskListView:focus, MarkdownPane:focus-within {
        border: round $accent;
    }

    ListItem {
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("tab", "focus_next", "Switch pane", show=True),
        Binding("shift+tab", "focus_previous", "Switch pane", show=False),
        Binding("right", "enter_project", "Open", show=True),
        Binding("left", "back", "Projects", show=True),
        Binding("l", "enter_project", "Open", show=False),
        Binding("h", "back", "Projects", show=False),
        Binding("c", "work_on_task", "Work (Claude)", show=True),
        Binding("R", "groom", "Review all", show=True),
        Binding("g", "mark_ongoing", "Ongoing", show=False),
        Binding("x", "mark_done", "Done", show=True),
        Binding("u", "mark_open", "Reopen", show=False),
        Binding("o", "toggle_closed", "Show closed", show=True),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit", show=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(
        self,
        projects: list[Project],
        workspace_name: str,
        workspace: bool,
        claude_cmd: list[str] | None = None,
        groom_cmd: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._projects = projects
        self._workspace_name = workspace_name
        self._workspace = workspace
        self._claude_cmd = claude_cmd or ["claude"]
        self._groom_cmd = groom_cmd or ["claude", "-p", "--dangerously-skip-permissions"]
        self._show_closed = False
        self._grooming = False
        self._level = "projects" if workspace else "tasks"
        self._current_project: Project | None = None
        self._last_project_name: str | None = None
        self._tasks_dir: Path | None = None
        self._tasks: list[Task] = []
        self._git_info: dict[Path, GitInfo | None] = {}

    @classmethod
    def single(
        cls,
        tasks_dir: Path,
        project_name: str,
        claude_cmd: list[str] | None = None,
        groom_cmd: list[str] | None = None,
    ) -> "TaskViewerApp":
        """Construct an app pinned to one project (no project-list level)."""
        project = Project(project_name, tasks_dir.parent, tasks_dir)
        return cls(
            [project], project_name, workspace=False,
            claude_cmd=claude_cmd, groom_cmd=groom_cmd,
        )

    def compose(self) -> ComposeResult:
        yield Header()
        yield TaskListView()
        with MarkdownPane():
            yield Markdown(_EMPTY_BODY)
        yield Footer()

    def on_mount(self) -> None:
        if self._level == "projects":
            self._show_projects()
        else:
            self._enter_project(self._projects[0])
        self.query_one(TaskListView).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]):
        """Hide keys that don't apply at the current navigation level."""
        if action == "enter_project":
            return True if self._level == "projects" else None
        if action == "back":
            return True if self._level == "tasks" and self._workspace else None
        if action in _TASK_ACTIONS:
            return True if self._level == "tasks" else None
        return True

    # --- navigation ------------------------------------------------------

    def action_enter_project(self) -> None:
        if self._level != "projects":
            return
        index = self.query_one(TaskListView).index
        if index is not None and 0 <= index < len(self._projects):
            self._enter_project(self._projects[index])

    def action_back(self) -> None:
        if self._level == "tasks" and self._workspace:
            self._show_projects()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._level == "projects":
            self.action_enter_project()

    def _enter_project(self, project: Project) -> None:
        self._current_project = project
        self._last_project_name = project.name
        self._tasks_dir = project.tasks_dir
        self._level = "tasks"
        self.title = f"tasks · {project.name}"
        self.refresh_bindings()
        self._refresh_tasks(keep_selection=False)

    def _show_projects(self) -> None:
        self._level = "projects"
        self._current_project = None
        self._tasks_dir = None
        self.title = f"projects · {self._workspace_name}"
        self.refresh_bindings()

        list_view = self.query_one(TaskListView)
        list_view.clear()
        for project in self._projects:
            row = _format_project_row(project, self._git_info.get(project.path))
            list_view.append(ListItem(Label(row)))
        self._update_projects_subtitle()

        index = _index_of(
            [p.name for p in self._projects], self._last_project_name
        )
        if self._projects:
            list_view.index = index
            self._show_project_summary(self._projects[index])
        list_view.focus()
        # Shelling out to git for every project would freeze the UI, so rows go
        # up with whatever was known last and are repainted when the scan lands.
        self._load_git_info()

    def _update_projects_subtitle(self) -> None:
        worktrees = sum(
            1 for info in self._git_info.values() if info and info.is_worktree
        )
        summary = f"{len(self._projects)} projects"
        if worktrees:
            summary += f" · {_plural(worktrees, 'worktree')}"
        self.sub_title = f"{summary} · → to open"

    @work(thread=True, group="git_info", exclusive=True)
    def _load_git_info(self) -> None:
        """Scan every project's git state off the event loop."""
        scanned = {project.path: load_git_info(project.path) for project in self._projects}
        self.call_from_thread(self._on_git_info, scanned)

    def _on_git_info(self, scanned: dict[Path, GitInfo | None]) -> None:
        self._git_info = scanned
        if self._level != "projects":
            return
        list_view = self.query_one(TaskListView)
        rows = list_view.query(Label)
        for project, label in zip(self._projects, rows):
            label.update(_format_project_row(project, scanned.get(project.path)))
        self._update_projects_subtitle()
        index = list_view.index
        if index is not None and 0 <= index < len(self._projects):
            self._show_project_summary(self._projects[index])

    # --- task actions ----------------------------------------------------

    def action_toggle_closed(self) -> None:
        self._show_closed = not self._show_closed
        self._refresh_tasks(keep_selection=True)

    def action_reload(self) -> None:
        self._refresh_tasks(keep_selection=True)

    def action_mark_ongoing(self) -> None:
        self._change_state("ongoing")

    def action_mark_done(self) -> None:
        self._change_state("closed")

    def action_mark_open(self) -> None:
        self._change_state("open")

    def action_work_on_task(self) -> None:
        """Mark the selected task ongoing, then launch Claude Code on it."""
        task = self._current_task()
        if task is None:
            return
        binary = self._claude_cmd[0]
        if shutil.which(binary) is None:
            self.notify(
                f"'{binary}' not found on PATH — set --claude-cmd or $TV_CLAUDE_CMD.",
                severity="error",
                timeout=6,
            )
            return

        new_path = self._change_state("ongoing", notify=False) or task.path
        self._launch_claude(task, new_path)
        self._refresh_tasks(keep_selection=True)

    def action_groom(self) -> None:
        """Kick off a background Claude Code pass to review/tidy all tasks."""
        if self._level != "tasks" or self._tasks_dir is None:
            return
        if self._grooming:
            self.notify("A task review is already running…")
            return
        binary = self._groom_cmd[0]
        if shutil.which(binary) is None:
            self.notify(
                f"'{binary}' not found on PATH — set --groom-cmd or $TV_GROOM_CMD.",
                severity="error",
                timeout=6,
            )
            return
        self._grooming = True
        self._update_subtitle()
        self.notify("Task review started in the background — keep browsing.")
        self._run_groom_worker()

    def action_cursor_down(self) -> None:
        self.query_one(TaskListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(TaskListView).action_cursor_up()

    # --- grooming worker -------------------------------------------------

    @work(thread=True, group="groom")
    def _run_groom_worker(self) -> None:
        assert self._tasks_dir is not None
        name = self._current_project.name if self._current_project else "tasks"
        log_path = Path(tempfile.gettempdir()) / f"tv-groom-{name}.log"
        result = run_groom(
            self._groom_cmd,
            self._tasks_dir.parent,
            self._tasks_dir.name,
            log_path=log_path,
        )
        self.call_from_thread(self._on_groom_finished, result)

    def _on_groom_finished(self, result: GroomResult) -> None:
        self._grooming = False
        self._refresh_tasks(keep_selection=True)
        if result.returncode == 0:
            self.notify("Task review complete — showing the summary.", timeout=6)
            report = result.output.strip() or "*The review made no changes.*"
            self.query_one(Markdown).update(f"# Task review\n\n{report}")
        else:
            where = f" See `{result.log_path}`." if result.log_path else ""
            self.notify(
                f"Task review exited with code {result.returncode}.{where}",
                severity="warning",
                timeout=8,
            )

    # --- data / rendering ------------------------------------------------

    def _current_task(self) -> Task | None:
        if self._level != "tasks":
            return None
        index = self.query_one(TaskListView).index
        if index is not None and 0 <= index < len(self._tasks):
            return self._tasks[index]
        return None

    def _change_state(self, new_state: str, *, notify: bool = True) -> Path | None:
        """Move the selected task to ``new_state``; return its new path."""
        task = self._current_task()
        if task is None or self._tasks_dir is None:
            return None
        if task.state == new_state:
            if notify:
                self.notify(f"{task.task_id} is already {new_state}.")
            return task.path
        try:
            new_path = set_state(task, new_state, self._tasks_dir)
        except StateChangeError as error:
            self.notify(str(error), severity="error", timeout=6)
            return None
        if notify:
            self.notify(f"{task.task_id} → {new_state}")
        self._refresh_tasks(keep_selection=True)
        return new_path

    def _launch_claude(self, task: Task, path: Path) -> None:
        """Suspend the TUI and run Claude Code on the task in the project root."""
        assert self._tasks_dir is not None
        project_root = self._tasks_dir.parent
        try:
            spec = path.relative_to(project_root)
        except ValueError:
            spec = path
        prompt = (
            "Please work on this task from the project's task tracker.\n\n"
            f"Task: {task.title}\n"
            f"Spec file: {spec}\n\n"
            f"Read {spec} in full for the details, then plan and implement it. "
            "The task has been marked as ongoing (moved to tasks/ongoing/); "
            "when it's complete, say so and I'll mark it done."
        )
        with self.suspend():
            try:
                subprocess.run([*self._claude_cmd, prompt], cwd=project_root)
            except FileNotFoundError:
                print(f"tv: could not run {self._claude_cmd[0]!r}.")
                input("Press Enter to return to tv...")

    def _refresh_tasks(self, *, keep_selection: bool) -> None:
        if self._tasks_dir is None:
            return
        states = STATES if self._show_closed else _ACTIVE_STATES
        previous_id = None
        if keep_selection and self._tasks:
            index = self.query_one(TaskListView).index
            if index is not None and 0 <= index < len(self._tasks):
                previous_id = self._tasks[index].task_id

        self._tasks = load_tasks(self._tasks_dir, states)
        list_view = self.query_one(TaskListView)
        list_view.clear()
        for task in self._tasks:
            list_view.append(ListItem(Label(_format_row(task))))

        self._update_subtitle()

        new_index = _index_of([t.task_id for t in self._tasks], previous_id)
        if self._tasks:
            list_view.index = new_index
            self._show_task(self._tasks[new_index])
        else:
            scope = "all" if self._show_closed else "active"
            self.query_one(Markdown).update(
                f"*No {scope} tasks in `{self._tasks_dir}`.*"
            )

    def _update_subtitle(self) -> None:
        counts = {state: 0 for state in STATES}
        for task in self._tasks:
            counts[task.state] = counts.get(task.state, 0) + 1
        scope = "all" if self._show_closed else "active"
        breakdown = " · ".join(
            f"{counts[s]} {s}" for s in STATES if counts[s] or not self._show_closed
        )
        subtitle = f"{len(self._tasks)} tasks ({scope}) · {breakdown}"
        if self._workspace:
            subtitle += "  · ← projects"
        if self._grooming:
            subtitle += "  ⟳ reviewing…"
        self.sub_title = subtitle

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is None:
            return
        if self._level == "projects":
            if 0 <= index < len(self._projects):
                self._show_project_summary(self._projects[index])
        elif 0 <= index < len(self._tasks):
            self._show_task(self._tasks[index])

    def _show_task(self, task: Task) -> None:
        header = f"# {task.title}\n\n"
        self.query_one(Markdown).update(header + _meta_line(task) + task.body)

    def _show_project_summary(self, project: Project) -> None:
        counts = count_states(project.tasks_dir)
        lines = [
            f"# {project.name}",
            "",
            f"`{project.path}`",
            "",
            f"**{counts['open']}** open · **{counts['ongoing']}** ongoing · "
            f"**{counts['closed']}** closed",
        ]
        lines += _git_section(self._git_info.get(project.path))
        active = load_tasks(project.tasks_dir, _ACTIVE_STATES)
        if active:
            lines += ["", "## Active tasks", ""]
            for task in active[:25]:
                lines.append(f"- {_STATE_MARK[task.state]} {task.title}")
            if len(active) > 25:
                lines.append(f"- …and {len(active) - 25} more")
        lines += ["", "*Press `→` or `Enter` to open.*"]
        self.query_one(Markdown).update("\n".join(lines))


def _format_row(task: Task) -> str:
    """Rich-markup label for one task row: state, priority, title."""
    mark = _STATE_MARK.get(task.state, "○")
    style = _PRIORITY_STYLE.get((task.priority or "").lower(), "")
    title = escape(task.title)
    body = f"[{style}]{title}[/]" if style else title
    return f"[dim]{mark}[/] {body}"


def _format_project_row(project: Project, info: GitInfo | None) -> str:
    """Two lines: the project and its task count, then its git state."""
    counts = count_states(project.tasks_dir)
    active = counts["open"] + counts["ongoing"]
    row = f"{escape(project.name)}  [dim]{active} active[/]"
    git_line = _format_git_line(info)
    return f"{row}\n{git_line}" if git_line else row


def _format_git_line(info: GitInfo | None) -> str:
    """Compact branch · drift · dirty · age line shown under a project row."""
    if info is None:
        return ""
    parts = [f"[dim]⎇ {escape(_shorten(info.branch, _ROW_BRANCH_WIDTH))}[/]"]
    if info.merged:
        # Nothing of its own left outside the base branch. For a worktree that
        # is the whole story — it can go — so `behind` would only be noise. A
        # main checkout in sync is the unremarkable case, so it says nothing.
        if info.is_worktree:
            parts.append("[green]✔merged[/]")
    else:
        if info.ahead:
            parts.append(f"[green]↑{info.ahead}[/]")
        if info.behind:
            parts.append(f"[dim]↓{info.behind}[/]")
    if info.dirty:
        style = "bold yellow" if info.merged else "cyan"
        parts.append(f"[{style}]✎{info.dirty}[/]")
    parts.append(f"[dim]{describe_age(info.updated)}[/]")
    return "  " + " ".join(parts)


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _meta_line(task: Task) -> str:
    """A small metadata line rendered above the task body."""
    parts: list[str] = [f"`{task.task_id}`", f"*{task.state}*"]
    if task.priority:
        parts.append(f"priority: {task.priority}")
    if task.labels:
        parts.append(" ".join(f"`{label}`" for label in task.labels))
    return " · ".join(parts) + "\n\n"


def _index_of(names: list[str], target: str | None) -> int:
    if target is not None and target in names:
        return names.index(target)
    return 0


def _git_section(info: GitInfo | None) -> list[str]:
    """Markdown lines: when the checkout was made, last touched, and its drift."""
    if info is None:
        return []
    heading = f"`{info.branch}`"
    if info.repo:
        heading += f" · worktree of `{info.repo}`"
    lines = [
        "",
        f"## {info.kind.capitalize()}",
        "",
        heading,
        "",
        # A worktree has a real birth date; a plain clone only knows when its
        # current branch started, which changes the moment you switch branches.
        f"- **{'Created' if info.is_worktree else 'Branch since'}** "
        f"{format_moment(info.created)}",
        f"- **Updated** {format_moment(info.updated)}{_dirty_note(info)}",
        f"- {_drift_note(info)}",
    ]
    if info.merged and info.is_worktree and info.dirty:
        lines += [
            "",
            f"> ⚠ Merged, but {_plural(info.dirty, 'file')} here "
            "are uncommitted — they exist nowhere else.",
        ]
    if info.subjects:
        lines += [
            "",
            f"### {_plural(info.ahead, 'commit')} not in `{info.base}`",
            "",
        ]
        lines += [f"- {message}" for message in info.subjects]
        if info.ahead > len(info.subjects):
            lines.append(f"- …and {info.ahead - len(info.subjects)} more")
    return lines


def _drift_note(info: GitInfo) -> str:
    """How this checkout stands against its base, as a whole bullet line."""
    if info.base is None:
        return "**Base** no `main`/`master` branch to compare against"
    if info.merged:
        # `ahead == 0` means the base already holds every commit here. For a
        # worktree that is the headline: the work landed, the directory can go.
        if info.is_worktree:
            return f"**Merged** fully into `{info.base}` — nothing of its own left"
        return f"**In sync** with `{info.base}` — nothing unpushed"
    parts = [f"**Base** `{info.base}`", f"**{info.ahead} ahead**"]
    if info.behind:
        parts.append(f"{info.behind} behind")
    return " · ".join(parts)


def _dirty_note(info: GitInfo) -> str:
    if not info.dirty:
        return ""
    return f" · {_plural(info.dirty, 'uncommitted file')}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
