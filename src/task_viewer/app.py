"""The Textual TUI: task list on the left, rendered markdown on the right."""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Label, ListItem, ListView, Markdown

from .discovery import STATES, Task, load_tasks

_PRIORITY_STYLE = {
    "high": "bold red",
    "medium": "yellow",
    "low": "dim",
}

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
        Binding("o", "toggle_closed", "Open/all", show=True),
        Binding("r", "reload", "Reload", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, tasks_dir: Path, project_name: str) -> None:
        super().__init__()
        self._tasks_dir = tasks_dir
        self._project_name = project_name
        self._show_closed = False
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

    def action_cursor_down(self) -> None:
        self.query_one(TaskListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(TaskListView).action_cursor_up()

    # --- data / rendering ------------------------------------------------

    def _refresh_tasks(self, *, keep_selection: bool) -> None:
        states = STATES if self._show_closed else ("open",)
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

        open_count = sum(1 for t in self._tasks if t.state == "open")
        scope = "open + closed" if self._show_closed else "open"
        self.sub_title = f"{len(self._tasks)} tasks ({scope}) · {open_count} open"

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
    mark = "○" if task.state == "open" else "●"
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
