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
            list_view.append(ListItem(Label(_format_project_row(project))))
        self.sub_title = f"{len(self._projects)} projects · → to open"

        index = _index_of(
            [p.name for p in self._projects], self._last_project_name
        )
        if self._projects:
            list_view.index = index
            self._show_project_summary(self._projects[index])
        list_view.focus()

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
            "",
            "Press `→` or `Enter` to open.",
        ]
        active = load_tasks(project.tasks_dir, _ACTIVE_STATES)
        if active:
            lines += ["", "## Active tasks", ""]
            for task in active[:25]:
                lines.append(f"- {_STATE_MARK[task.state]} {task.title}")
            if len(active) > 25:
                lines.append(f"- …and {len(active) - 25} more")
        self.query_one(Markdown).update("\n".join(lines))


def _format_row(task: Task) -> str:
    """Rich-markup label for one task row: state, priority, title."""
    mark = _STATE_MARK.get(task.state, "○")
    style = _PRIORITY_STYLE.get((task.priority or "").lower(), "")
    title = escape(task.title)
    body = f"[{style}]{title}[/]" if style else title
    return f"[dim]{mark}[/] {body}"


def _format_project_row(project: Project) -> str:
    counts = count_states(project.tasks_dir)
    active = counts["open"] + counts["ongoing"]
    return f"{escape(project.name)}  [dim]{active} active[/]"


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
