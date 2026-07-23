"""The Textual TUI: task list on the left, rendered markdown on the right."""

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

from .discovery import STATES, Task, load_tasks
from .groom import GroomResult, run_groom
from .state_ops import StateChangeError, set_state

_PRIORITY_STYLE = {
    "high": "bold red",
    "medium": "yellow",
    "low": "dim",
}

# Active states are shown by default; closed is folded in with the `o` toggle.
_ACTIVE_STATES = ("open", "ongoing")

_STATE_MARK = {"open": "○", "ongoing": "◐", "closed": "●"}

_EMPTY_BODY = "*Select a task on the left. Press `Tab` to move between panes.*"


class TaskListView(ListView):
    """Left pane. Nothing extra yet, but a named subclass keeps CSS targeted."""


class MarkdownPane(VerticalScroll):
    """Right pane. Focusable so `Tab` reaches it and arrows scroll it."""

    can_focus = True


class TaskViewerApp(App):
    """Browse the markdown task files of a single gimle project."""

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
        tasks_dir: Path,
        project_name: str,
        claude_cmd: list[str] | None = None,
        groom_cmd: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._tasks_dir = tasks_dir
        self._project_name = project_name
        self._claude_cmd = claude_cmd or ["claude"]
        self._groom_cmd = groom_cmd or ["claude", "-p", "--dangerously-skip-permissions"]
        self._show_closed = False
        self._grooming = False
        self._tasks: list[Task] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield TaskListView()
        with MarkdownPane():
            yield Markdown(_EMPTY_BODY)
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"tasks · {self._project_name}"
        self._refresh_tasks(keep_selection=False)
        self.query_one(TaskListView).focus()

    # --- actions ---------------------------------------------------------

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

    # --- data / rendering ------------------------------------------------

    def _update_subtitle(self) -> None:
        counts = {state: 0 for state in STATES}
        for task in self._tasks:
            counts[task.state] = counts.get(task.state, 0) + 1
        scope = "all" if self._show_closed else "active"
        breakdown = " · ".join(
            f"{counts[s]} {s}" for s in STATES if counts[s] or not self._show_closed
        )
        subtitle = f"{len(self._tasks)} tasks ({scope}) · {breakdown}"
        if self._grooming:
            subtitle += "  ⟳ reviewing…"
        self.sub_title = subtitle

    @work(thread=True, group="groom")
    def _run_groom_worker(self) -> None:
        log_path = Path(tempfile.gettempdir()) / f"tv-groom-{self._project_name}.log"
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

    def _current_task(self) -> Task | None:
        index = self.query_one(TaskListView).index
        if index is not None and 0 <= index < len(self._tasks):
            return self._tasks[index]
        return None

    def _change_state(self, new_state: str, *, notify: bool = True) -> Path | None:
        """Move the selected task to ``new_state``; return its new path."""
        task = self._current_task()
        if task is None:
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

        new_index = _restore_index(self._tasks, previous_id)
        if self._tasks:
            list_view.index = new_index
            self._show_task(self._tasks[new_index])
        else:
            self.query_one(Markdown).update(
                f"*No {scope} tasks found in `{self._tasks_dir}`.*"
            )

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._tasks):
            self._show_task(self._tasks[index])

    def _show_task(self, task: Task) -> None:
        header = f"# {task.title}\n\n"
        meta_line = _meta_line(task)
        self.query_one(Markdown).update(header + meta_line + task.body)


def _format_row(task: Task) -> str:
    """Rich-markup label for one list row: state, priority, title."""
    mark = _STATE_MARK.get(task.state, "○")
    style = _PRIORITY_STYLE.get((task.priority or "").lower(), "")
    title = escape(task.title)
    body = f"[{style}]{title}[/]" if style else title
    return f"[dim]{mark}[/] {body}"


def _meta_line(task: Task) -> str:
    """A small italic metadata line rendered above the task body."""
    parts: list[str] = [f"`{task.task_id}`", f"*{task.state}*"]
    if task.priority:
        parts.append(f"priority: {task.priority}")
    if task.labels:
        parts.append(" ".join(f"`{label}`" for label in task.labels))
    return " · ".join(parts) + "\n\n"


def _restore_index(tasks: list[Task], previous_id: str | None) -> int:
    if previous_id is None:
        return 0
    for i, task in enumerate(tasks):
        if task.task_id == previous_id:
            return i
    return 0
