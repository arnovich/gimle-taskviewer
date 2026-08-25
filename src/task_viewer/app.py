"""The Textual TUI.

Two navigation levels:

* **projects** — when ``tv`` is run from a workspace root, the left pane lists
  the child projects that have a tasks folder. ``→``/Enter steps into one.
* **tasks** — the task list on the left, rendered markdown on the right.
  ``←`` steps back to the project list (in workspace mode).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView, Markdown

from .discovery import STATES, Task, count_states, load_tasks
from .git_info import (
    GitInfo,
    describe_age,
    describe_age_phrase,
    format_moment,
    load_git_info,
)
from .groom import GroomResult, run_groom
from .pull_requests import (
    ActionResult,
    open_in_browser,
    PullRequest,
    checks_summary,
    comment as post_comment,
    load_pull_requests,
    merge as merge_pull_request,
)
from .queue_ops import QueueError, clear_next, enqueue, promote
from .remote import UpdateResult, cancel_all, fast_forward, fetch
from .state_ops import StateChangeError, set_state
from .workspace import Project, ProjectGroup, group_projects

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
        "queue_task",
        "promote_task",
        "unqueue_task",
        "mark_ongoing",
        "mark_done",
        "mark_open",
        "toggle_closed",
        "reload",
    }
)

_EMPTY_BODY = "*Select a task on the left. Press `Tab` to move between panes.*"

# Branch names are long ("feat/strict_rewrite_proof_kernel") and the list pane is
# narrow, so rows show a truncated form and the summary pane the full one. A
# worktree row is indented under its repo and gets correspondingly less room.
_ROW_BRANCH_WIDTH = 20
_WORKTREE_NAME_WIDTH = 18

# Fences off the editor template. An HTML comment, not `#` lines: this is a
# Markdown box, where `## Blocking` is a heading and `#538` is a pull request,
# and stripping those silently eats the review.
_COMMENT_FENCE = "<!-- tv:"


@dataclass
class _Row:
    """One visible entry in the project list: a repo, or one of its worktrees."""

    project: Project
    group: ProjectGroup
    is_worktree: bool


class TaskListView(ListView):
    """Left pane. Nothing extra yet, but a named subclass keeps CSS targeted."""


class MarkdownPane(VerticalScroll):
    """Right pane. Focusable so `Tab` reaches it and arrows scroll it."""

    can_focus = True


class ConfirmMerge(ModalScreen[str]):
    """Merging is outward-facing and hard to undo, so it is never one keypress."""

    BINDINGS = [
        Binding("escape", "dismiss_merge", "Cancel"),
        Binding("n", "dismiss_merge", "Cancel", show=False),
        Binding("q", "dismiss_merge", "Cancel", show=False),
        Binding("y", "confirm('merge')", "Merge"),
    ]

    def __init__(self, repo: str, pull: PullRequest) -> None:
        super().__init__()
        self._repo = repo
        self._pull = pull

    def compose(self) -> ComposeResult:
        pull = self._pull
        lines = [
            f"Merge #{pull.number} into {_literal(self._repo)}?",
            "",
            # A PR title is arbitrary text: unescaped, "[WIP] …" silently
            # vanishes and "[/]" takes the app down.
            f"  {_literal(_shorten(pull.title, 52))}",
            f"  {_literal(pull.diffstat)} · {_literal(checks_summary(pull))}",
        ]
        for warning in pull.warnings:
            lines.append(f"  [yellow]⚠ {_literal(warning)}[/]")
        # The comments are why you would change your mind, so they go above
        # the keys rather than four screens down the other pane.
        recent = pull.human_comments
        if recent:
            latest = recent[-1].body.strip().splitlines()[0] if recent[-1].body.strip() else ""
            lines += [
                f"  [bold]{_plural(len(recent), 'comment')}[/], latest:",
                f"  [bold]“{_literal(_shorten(latest, 46))}”[/]",
            ]
        lines += ["", "y  merge      esc  cancel"]
        with VerticalScroll(id="confirm"):
            yield Label("\n".join(lines))

    def action_confirm(self, choice: str) -> None:
        self.dismiss(choice)

    def action_dismiss_merge(self) -> None:
        self.dismiss(None)


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

    ConfirmMerge {
        align: center middle;
    }

    ConfirmMerge #confirm {
        width: 64;
        height: auto;
        max-height: 80%;
        border: round $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("tab", "focus_next", "Switch pane", show=True),
        Binding("shift+tab", "focus_previous", "Switch pane", show=False),
        Binding("right", "enter_project", "Open", show=True),
        Binding("left", "back", "Projects", show=True),
        Binding("l", "enter_project", "Open", show=False),
        Binding("h", "back", "Projects", show=False),
        Binding("space", "toggle_group", "Fold", show=True),
        Binding("f", "fetch", "Fetch", show=True),
        Binding("M", "merge_pr", "Merge PR", show=True),
        Binding("m", "comment_pr", "Comment", show=True),
        Binding("w", "open_pr", "Open in browser", show=True),
        Binding("u", "update", "Update", show=True),
        Binding("c", "work_on_task", "Work (Claude)", show=True),
        Binding("R", "groom", "Review all", show=True),
        Binding("n", "queue_task", "Queue next", show=True),
        Binding("p", "promote_task", "Queue first", show=False),
        Binding("N", "unqueue_task", "Unqueue", show=False),
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
        self._groups = group_projects(projects) if workspace else []
        self._rows: list[_Row] = []
        # The selection the app intends, tracked here rather than read back from
        # the ListView, whose index is transiently None while it re-populates.
        self._selected_path: Path | None = None
        self._rebuilding = False
        self._expanded: set[Path] = set()
        self._fetching = False
        self._unreachable: set[Path] = set()
        self._updating = False
        self._pulls: dict[Path, PullRequest] = {}
        self._pr_errors: set[Path] = set()
        self._merging = False

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

    def on_unmount(self) -> None:
        """Kill any in-flight git child so `q` never waits on the network."""
        cancel_all()

    def check_action(self, action: str, parameters: tuple[object, ...]):
        """Hide keys that don't apply at the current navigation level."""
        if action == "enter_project":
            return True if self._level == "projects" else None
        if action == "back":
            return True if self._level == "tasks" and self._workspace else None
        if action in ("merge_pr", "comment_pr", "open_pr"):
            # Only offered where there is actually a PR to act on.
            row = self._current_row()
            return True if row and row.project.path in self._pulls else None
        if action in ("fetch", "update"):
            return True if self._level == "projects" else None
        if action == "toggle_group":
            # Advertising a key that does nothing on this row reads as a bug.
            row = self._current_row()
            return True if row is not None and row.group.worktrees else None
        if action in _TASK_ACTIONS:
            return True if self._level == "tasks" else None
        return True

    # --- navigation ------------------------------------------------------

    def action_enter_project(self) -> None:
        row = self._current_row()
        if row is not None:
            self._enter_project(row.project)

    def action_toggle_group(self) -> None:
        """Fold or unfold the highlighted repo's worktrees."""
        row = self._current_row()
        if row is None or not row.group.has_worktrees:
            return
        # On a worktree row, fold the repo it belongs to rather than nothing.
        key = row.group.project.path
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self._build_project_rows(keep=key)

    def action_fetch(self) -> None:
        """Ask every remote what it has, then repaint."""
        if self._level != "projects" or self._fetching:
            return
        self._fetching = True
        self._update_projects_subtitle()
        self._load_git_info(refresh=True)

    def _on_pulls(self, scanned: tuple[dict[Path, PullRequest], set[Path]]) -> None:
        self._pulls, self._pr_errors = scanned
        if self._level == "projects" and self.is_running:
            self._build_project_rows()

    def _on_fetch_finished(self, unreachable: set[Path]) -> None:
        self._fetching = False
        self._unreachable = unreachable
        # The sweep can land while the app is shutting down.
        if self._level == "projects" and self.is_running:
            self._build_project_rows(keep=self._current_path())
        self._update_projects_subtitle()

    def action_update(self) -> None:
        """Fast-forward the highlighted project to its upstream."""
        row = self._current_row()
        if row is None:
            return
        if self._updating:
            return
        self._updating = True
        self.notify(f"{row.project.name}: updating…", timeout=2)
        self._run_update(row.project)

    @work(thread=True, group="update", exclusive=True, exit_on_error=False)
    def _run_update(self, project: Project) -> None:
        """Fetch and fast-forward off the event loop.

        ``git merge`` runs the repository's ``post-merge`` hook — arbitrary code
        — so it must not happen with the UI frozen.
        """
        self.call_from_thread(self._on_update_finished, project, fast_forward(project.path))

    def _on_update_finished(self, project: Project, result: UpdateResult) -> None:
        self._updating = False
        self.notify(
            f"{project.name}: {result.message}",
            severity="information" if result.ok else "warning",
            timeout=8,
        )
        # The tasks travelled with the commits, so the list is stale too.
        self._load_git_info()

    def action_merge_pr(self) -> None:
        """Merge the highlighted worktree's pull request, after confirming."""
        row = self._current_row()
        pull = self._pulls.get(row.project.path) if row else None
        if row is None or pull is None or self._merging:
            return
        blocking = pull.blocking
        if blocking:
            # Not a warning: GitHub will refuse, and so would --admin.
            self.notify(
                f"#{pull.number} cannot be merged — {blocking}",
                severity="warning",
                timeout=8,
            )
            return
        self.push_screen(
            ConfirmMerge(row.group.project.name, pull),
            lambda choice: self._start_merge(row, pull, choice),
        )

    def _start_merge(self, row: _Row, pull: PullRequest, choice: str | None) -> None:
        if choice != "merge" or self._merging:
            return
        self._merging = True
        self.notify(f"merging #{pull.number}…", timeout=3)
        self._run_merge(row.group.project, pull)

    @work(thread=True, group="pr", exclusive=True, exit_on_error=False)
    def _run_merge(self, repo: Project, pull: PullRequest) -> None:
        """Off the event loop: a merge is two network round trips."""
        self.call_from_thread(
            self._on_pr_action, merge_pull_request(repo.path, pull.number), True
        )

    def action_open_pr(self) -> None:
        """Open the highlighted worktree's pull request in a browser."""
        row = self._current_row()
        pull = self._pulls.get(row.project.path) if row else None
        if pull is None:
            return
        self._open_url(pull.url)

    def _open_url(self, url: str) -> None:
        result = open_in_browser(url)
        self.notify(
            result.message,
            severity="information" if result.ok else "warning",
            timeout=8 if result.ok else 12,
        )

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Make the link in the pane a link, rather than text to select."""
        event.prevent_default()
        self._open_url(event.href)

    def action_comment_pr(self) -> None:
        """Write a comment in $EDITOR and post it to the pull request."""
        row = self._current_row()
        pull = self._pulls.get(row.project.path) if row else None
        if row is None or pull is None or self._merging:
            return
        try:
            draft = self._compose(pull)
        except Exception as error:  # noqa: BLE001 - never take the TUI down
            self.notify(f"could not open an editor: {error}", severity="error", timeout=8)
            return
        if draft is None:
            self.notify(
                "no comment written — if your $EDITOR detaches (code, subl), "
                "give it a blocking form such as `code --wait`",
                timeout=8,
            )
            return
        body, draft_path = draft
        self._merging = True
        self._run_comment(row.group.project, pull, body, draft_path)

    @work(thread=True, group="pr", exclusive=True, exit_on_error=False)
    def _run_comment(
        self, repo: Project, pull: PullRequest, body: str, draft_path: Path
    ) -> None:
        result = post_comment(repo.path, pull.number, body)
        if result.ok:
            draft_path.unlink(missing_ok=True)
        else:
            # Never destroy what the user wrote; tell them where it is.
            result = ActionResult(result.ok, f"{result.message} — draft kept at {draft_path}")
        self.call_from_thread(self._on_pr_action, result, True)

    def _on_pr_action(self, result: ActionResult, refresh: bool) -> None:
        self._merging = False
        self.notify(
            result.message,
            severity="information" if result.ok else "warning",
            timeout=10,
        )
        if refresh and self.is_running:
            # Re-scan either way: a timeout does not mean the call failed.
            self._load_git_info(refresh=True)

    def _compose(self, pull: PullRequest) -> tuple[str, Path] | None:
        """Suspend the TUI, let $EDITOR take the comment, return it and its file.

        The template is fenced off with an HTML comment rather than ``#`` lines:
        this is a Markdown box, where ``## Blocking`` is a heading and ``#538``
        is a pull request, and stripping them silently eats the review.
        """
        template = (
            f"\n\n{_COMMENT_FENCE}\n"
            f"Comment on #{pull.number}: {pull.title}\n"
            "Everything from the marker down is ignored. An empty message aborts.\n"
            "-->\n"
        )
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".md", prefix="tv-comment-", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write(template)
        path = Path(handle.name)

        try:
            with self.suspend():
                finished = _run_editor(editor, path)
        except SuspendNotSupported:
            # Headless, or a host with no terminal to step out of: there is no
            # full-screen UI in the way, so just run it.
            finished = _run_editor(editor, path)
        if finished is None or finished.returncode != 0:
            # `vim :cq` is the conventional "abort this" and must be honoured.
            path.unlink(missing_ok=True)
            return None

        written = path.read_text(encoding="utf-8", errors="replace")
        body = written.split(_COMMENT_FENCE, 1)[0].strip()
        if not body or written == template:
            path.unlink(missing_ok=True)
            return None
        return body, path

    def _current_path(self) -> Path | None:
        row = self._current_row()
        return row.project.path if row else None

    def _current_row(self) -> _Row | None:
        if self._level != "projects" or not self._rows:
            return None
        index = self.query_one(TaskListView).index
        if index is not None and 0 <= index < len(self._rows):
            return self._rows[index]
        return None

    def action_back(self) -> None:
        if self._level == "tasks" and self._workspace:
            self._show_projects()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._level == "projects":
            self.action_enter_project()

    def _enter_project(self, project: Project) -> None:
        # The list is about to hold task rows, not project rows; forgetting them
        # keeps the repaint-in-place path below from writing into the wrong list.
        self._rows = []
        self._current_project = project
        self._last_project_name = project.name
        self._tasks_dir = project.tasks_dir
        self._level = "tasks"
        self.title = f"tasks · {project.name}"
        self.refresh_bindings()
        self._refresh_tasks(keep_selection=False)

    def _show_projects(self) -> None:
        self._level = "projects"
        # Set before the rows paint, so the subtitle shows it from the start.
        first_look = not self._git_info
        self._fetching = self._fetching or first_look
        self._current_project = None
        self._tasks_dir = None
        self.title = f"projects · {self._workspace_name}"
        self.refresh_bindings()

        self._build_project_rows(keep=self._last_project_path())
        self.query_one(TaskListView).focus()
        # Shelling out to git for every project would freeze the UI, so rows go
        # up with whatever was known last and are repainted when the scan lands.
        self._load_git_info(refresh=first_look)

    def _last_project_path(self) -> Path | None:
        for project in self._projects:
            if project.name == self._last_project_name:
                return project.path
        return None

    def _build_project_rows(self, *, keep: Path | None = None) -> None:
        """Rebuild the visible rows from the groups and what is expanded."""
        structure = [row.project.path for row in self._rows]
        self._rows = []
        for group in self._groups:
            self._rows.append(_Row(group.project, group, is_worktree=False))
            if group.project.path in self._expanded:
                for worktree in group.worktrees:
                    self._rows.append(_Row(worktree, group, is_worktree=True))

        list_view = self.query_one(TaskListView)
        # Prefer the row the caller named; fall back to wherever the cursor
        # already is, so a background rebuild never teleports it to the top.
        index = None
        for candidate in (keep, self._selected_path):
            if candidate is None:
                continue
            index = next(
                (i for i, r in enumerate(self._rows) if r.project.path == candidate),
                None,
            )
            if index is not None:
                break
        if index is None:
            index = 0
        index = min(index, len(self._rows) - 1) if self._rows else 0

        if self._rows:
            self._selected_path = self._rows[index].project.path

        # Same rows, different text — the common case, since the background git
        # scan repaints without changing what is listed. Updating labels in
        # place is synchronous and cannot race.
        reusable = structure and len(list_view) == len(self._rows)
        if reusable and [row.project.path for row in self._rows] == structure:
            self._rebuilding = True
            try:
                for row, label in zip(self._rows, list_view.query(Label)):
                    label.update(self._row_label(row))
                if self._rows:
                    _select(list_view, index)
            finally:
                self._rebuilding = False
            self._update_projects_subtitle()
            if self._rows:
                self._show_project_summary(self._rows[index].project)
            return

        # The row set actually changed, so the list has to be rebuilt. Do it in
        # a coroutine that AWAITS the removal: ListView.clear() prunes
        # asynchronously, and every attempt to work around that timing rather
        # than wait for it left the cursor bar on a widget already on its way out.
        self.call_next(self._repopulate_rows, index)

    async def _repopulate_rows(self, index: int) -> None:
        """Replace every row, waiting for the old ones to actually be gone."""
        list_view = self.query_one(TaskListView)
        self._rebuilding = True
        try:
            await list_view.clear()
            await list_view.extend(
                ListItem(Label(self._row_label(row))) for row in self._rows
            )
            if self._rows:
                index = min(index, len(self._rows) - 1)
                _select(list_view, index)
        finally:
            self._rebuilding = False
        self._update_projects_subtitle()
        if self._rows:
            self._show_project_summary(self._rows[index].project)

    def _row_label(self, row: _Row) -> str:
        info = self._git_info.get(row.project.path)
        pull = self._pulls.get(row.project.path)
        if row.is_worktree:
            return _format_worktree_row(row.project, row.group.project, info, pull)
        if not row.group.worktrees:
            return _format_project_row(row.project, info, pull=pull)
        expanded = row.group.project.path in self._expanded
        note = ""
        if not expanded:
            note = _format_folded_note(
                [self._git_info.get(w.path) for w in row.group.worktrees],
                [self._pulls.get(w.path) for w in row.group.worktrees],
            )
        marker = "[dim]▾[/] " if expanded else "[dim]▸[/] "
        return _format_project_row(row.project, info, marker, note, pull)

    def _update_projects_subtitle(self) -> None:
        worktrees = sum(len(group.worktrees) for group in self._groups)
        summary = _plural(len(self._groups), "repo" if worktrees else "project")
        if worktrees:
            summary += f" · {_plural(worktrees, 'worktree')}"
        if self._pulls:
            summary += f" · {_plural(len(self._pulls), 'PR')}"
        if self._pr_errors:
            summary += "  ⚠ gh unavailable"
        if self._fetching:
            summary += "  ⟳ fetching…"
        self.sub_title = f"{summary} · → to open"

    @work(thread=True, group="git_info", exclusive=True, exit_on_error=False)
    def _load_git_info(self, refresh: bool = False) -> None:
        """Scan every project's git state off the event loop.

        With ``refresh`` the remotes are contacted first, so the counts reflect
        the remote as it is now rather than as of the last fetch. Rows are
        painted from the local scan before that starts, so the network never
        holds up the list.
        """
        self.call_from_thread(self._on_git_info, self._scan())
        if not refresh:
            return
        # One fetch per repository: a worktree shares its repo's object store,
        # so fetching each of them separately would just repeat the round trip.
        repos = [group.project for group in self._groups]
        with ThreadPoolExecutor(max_workers=8) as pool:
            reached = list(pool.map(fetch, [repo.path for repo in repos]))
        try:
            self.call_from_thread(self._on_pulls, self._scan_pulls())
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            self.call_from_thread(
                self.notify, f"could not list pull requests: {error}", severity="warning"
            )
        # A failed fetch still bumps FETCH_HEAD's mtime, so without this the
        # pane would report "checked just now" for a remote never contacted.
        unreachable = {
            project.path
            for group, ok in zip(self._groups, reached)
            if not ok
            for project in (group.project, *group.worktrees)
        }
        self.call_from_thread(self._on_git_info, self._scan())
        self.call_from_thread(self._on_fetch_finished, unreachable)

    def _scan(self) -> dict[Path, GitInfo | None]:
        return {project.path: load_git_info(project.path) for project in self._projects}

    def _scan_pulls(self) -> tuple[dict[Path, PullRequest], set[Path]]:
        """Open PRs for every project, keyed by the checkout they belong to.

        One `gh` call per repository — a worktree's PR is listed by its repo,
        so asking each worktree separately would just repeat the round trip.
        """
        found: dict[Path, PullRequest] = {}
        unreachable: set[Path] = set()
        for group in self._groups:
            listing = load_pull_requests(group.project.path)
            if not listing.reached:
                # Rendering nothing would look exactly like a clear queue.
                unreachable.add(group.project.path)
                continue
            for project in (group.project, *group.worktrees):
                info = self._git_info.get(project.path)
                if info and info.branch in listing.by_branch:
                    found[project.path] = listing.by_branch[info.branch]
        return found, unreachable

    def _on_git_info(self, scanned: dict[Path, GitInfo | None]) -> None:
        self._git_info = scanned
        # The scan can land after the user has stepped into a project, or while
        # the app is shutting down — in both cases there is nothing to repaint.
        if self._level != "projects" or not self.is_running:
            return
        # Rebuild rather than zip labels: ListView.clear() prunes
        # asynchronously, so a scan landing mid-prune would write into widgets
        # that are about to vanish and leave the survivors showing stale counts.
        self._build_project_rows(keep=self._current_path())

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

    def action_queue_task(self) -> None:
        """Add the selected task to the end of the owner's work queue."""
        self._change_queue(enqueue, "queued")

    def action_promote_task(self) -> None:
        """Move the selected task to the front of the work queue."""
        self._change_queue(promote, "queued first")

    def action_unqueue_task(self) -> None:
        """Take the selected task out of the work queue."""
        self._change_queue(None, "unqueued")

    def _change_queue(self, operation, verb: str) -> None:
        task = self._current_task()
        if task is None or self._tasks_dir is None:
            return
        try:
            if operation is None:
                clear_next(task, self._tasks_dir)
                detail = ""
            else:
                detail = f" as #{operation(task, self._tasks_dir)}"
        except (QueueError, OSError) as error:
            # A read-only or vanished task file must not take the app down.
            self.notify(str(error), severity="error", timeout=6)
            return
        self.notify(f"{task.task_id} {verb}{detail}")
        self._refresh_tasks(keep_selection=True)

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

    @work(thread=True, group="groom", exit_on_error=False)
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
        number_width = _number_width(self._tasks)
        list_view = self.query_one(TaskListView)
        list_view.clear()
        for task in self._tasks:
            list_view.append(ListItem(Label(_format_row(task, number_width))))

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
            if self._rebuilding:
                return
            if 0 <= index < len(self._rows):
                self._selected_path = self._rows[index].project.path
                self._show_project_summary(self._rows[index].project)
            self.refresh_bindings()  # `space` only applies on a repo with worktrees
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
        lines += _git_section(
            self._git_info.get(project.path), project.path in self._unreachable
        )
        lines += _pull_request_section(self._pulls.get(project.path))
        active = load_tasks(project.tasks_dir, _ACTIVE_STATES)
        if active:
            lines += ["", "## Active tasks", ""]
            for task in active[:25]:
                number = f"`{task.number}` " if task.number else ""
                lines.append(f"- {_STATE_MARK[task.state]} {number}{task.title}")
            if len(active) > 25:
                lines.append(f"- …and {len(active) - 25} more")
        lines += ["", "*Press `→` or `Enter` to open.*"]
        self.query_one(Markdown).update("\n".join(lines))


def _format_row(task: Task, number_width: int) -> str:
    """Rich-markup label for one task row: state, id, queue rank, title.

    ``number_width`` is the widest id number in the list; a task without one is
    padded to the same width so every title starts in the same column.
    """
    mark = _STATE_MARK.get(task.state, "○")
    style = _PRIORITY_STYLE.get((task.priority or "").lower(), "")
    title = escape(task.title)
    body = f"[{style}]{title}[/]" if style else title
    rank = f"[bold cyan]{task.next_rank}[/] " if task.next_rank else ""
    if not number_width:
        return f"[dim]{mark}[/] {rank}{body}"
    return f"[dim]{mark} {(task.number or '').rjust(number_width)}[/] {rank}{body}"


def _format_project_row(
    project: Project,
    info: GitInfo | None,
    marker: str = "  ",
    note: str = "",
    pull: PullRequest | None = None,
) -> str:
    """Two lines: the project and its task count, then its git state.

    ``marker`` is the fold column, always two wide so names stay in one column
    whether or not a repo has worktrees. ``note`` summarises the worktrees
    folded away underneath, and leads the second line because it is what has to
    survive a narrow pane.
    """
    counts = count_states(project.tasks_dir)
    active = counts["open"] + counts["ongoing"]
    row = f"{marker}{escape(project.name)}  [dim]{active} active[/]"
    state = (_format_pr_marker(pull).strip() + " " + _format_git_line(info).strip()).strip()
    if note:
        state = f"{note} · {state}" if state else note
    return f"{row}\n  {state}" if state else row


def _format_folded_note(
    worktrees: list[GitInfo | None], pulls: list[PullRequest | None] | None = None
) -> str:
    """``3 wt ✎2 ✔3⚠`` — what is folded away, most decision-relevant first.

    A merged worktree can be deleted; a merged worktree holding uncommitted
    files cannot, and that warning is the one thing that must not be the first
    casualty of a narrow pane.
    """
    known = [info for info in worktrees if info is not None]
    merged = [info for info in known if info.merged]
    dirty = sum(1 for info in known if info.dirty)
    unpushed = sum(1 for info in known if info.unpushed or _never_pushed(info))
    parts = [f"[dim]{len(worktrees)} wt[/]"]
    open_prs = [pull for pull in (pulls or []) if pull is not None]
    if open_prs:
        # Otherwise the only way to learn a PR is waiting is to expand and look.
        style = "bold red" if any(p.blocking for p in open_prs) else "green"
        parts.append(f"[{style}]{len(open_prs)} PR[/]")
    if dirty:
        parts.append(f"[cyan]✎{dirty}[/]")
    if unpushed:
        parts.append(f"[yellow]↑{unpushed}[/]")
    if merged:
        risky = any(info.dirty for info in merged)
        style = "bold yellow" if risky else "green"
        parts.append(f"[{style}]✔{len(merged)}{'⚠' if risky else ''}[/]")
    return " ".join(parts)


def _never_pushed(info: GitInfo) -> bool:
    return info.has_remote and info.upstream is None and not info.merged


def _format_worktree_row(
    project: Project,
    repo: Project,
    info: GitInfo | None,
    pull: PullRequest | None = None,
) -> str:
    """One line per worktree, indented a level deeper than its repo's own line.

    The folder suffix is the identity rather than the branch: it is what you
    ``cd`` to and what ``git worktree remove`` takes, and this workspace puts
    the task number there (``gimle-mimir-166``) where the branch has none.
    """
    name = _worktree_suffix(project.name, repo.name)
    shown = escape(_shorten(name, _WORKTREE_NAME_WIDTH))
    if info is None:
        return f"    [dim]{shown}[/]"
    return f"    {shown}{_format_pr_marker(pull)}{_format_state(info)}"


def _format_pr_marker(pull: PullRequest | None) -> str:
    """``#453`` — red when it cannot land, yellow when it needs a look."""
    if pull is None:
        return ""
    if pull.blocking:
        style = "bold red"
    elif pull.warnings:
        style = "yellow"
    else:
        style = "green"
    return f" [{style}]#{pull.number}[/]"


def _worktree_suffix(name: str, repo: str) -> str:
    """``gimle-mimir-166`` under ``gimle-mimir`` is just ``166``."""
    if name.startswith(repo) and len(name) > len(repo):
        return name[len(repo):].lstrip("-_") or name
    return name


def _format_git_line(info: GitInfo | None, width: int = _ROW_BRANCH_WIDTH) -> str:
    """Compact branch · drift · dirty · age line shown under a project row."""
    if info is None:
        return ""
    branch = escape(_shorten(info.branch, width))
    return f"  [dim]⎇ {branch}[/]{_format_state(info)}"


def _format_state(info: GitInfo) -> str:
    """The drift, dirty and age markers — everything except the branch."""
    parts: list[str] = []
    if info.merged and info.is_worktree:
        # Nothing of its own left outside the base branch — it can go.
        parts.append("[green]✔merged[/]")
    # Remote state first: it is the most actionable, and a narrow pane clips
    # from the right. Age goes last for the same reason.
    parts += _remote_markers(info)
    if info.dirty:
        style = "bold yellow" if info.merged else "cyan"
        parts.append(f"[{style}]✎{info.dirty}[/]")
    if not info.merged:
        # Distance from the base branch, in ASCII so it cannot be confused with
        # the remote arrows above — and so it renders in any terminal font.
        if info.ahead:
            parts.append(f"[green]+{info.ahead}[/]")
        if info.behind:
            parts.append(f"[dim]-{info.behind}[/]")
    parts.append(f"[dim]{describe_age(info.updated)}[/]")
    return " " + " ".join(parts)


def _remote_markers(info: GitInfo) -> list[str]:
    """``↑2`` to push, ``↓3`` to pull — where this sits against its remote.

    Silent when there is nothing to act on. The arrows are U+2191/2193, which
    terminal fonts actually carry; the doubled forms are in almost none.
    """
    if info.upstream is None:
        # Never pushed, but only worth saying when there is somewhere to push.
        return ["[yellow]new[/]"] if info.has_remote and not info.merged else []
    if info.upstream_gone:
        # Published, then deleted upstream: the work landed and was tidied up.
        return ["[green]gone[/]"]
    markers = []
    if info.unpushed:
        markers.append(f"[yellow]↑{info.unpushed}[/]")
    if info.unpulled and info.tracks_own_branch:
        markers.append(f"[bold cyan]↓{info.unpulled}[/]")
    return markers


def _run_editor(editor: str, path: Path) -> subprocess.CompletedProcess | None:
    """Hand the file to $EDITOR; ``None`` if it could not be started."""
    try:
        return subprocess.run([*shlex.split(editor), str(path)])
    except (OSError, ValueError):
        return None


def _literal(text: str) -> str:
    """Render ``text`` as itself, whatever brackets it contains.

    ``rich.markup.escape`` is not enough: its tag pattern only matches
    lowercase, so it leaves ``[WIP]`` alone while the renderer still eats it —
    a PR title silently loses its prefix in the dialog asking about it.
    """
    return text.replace("\\", "\\\\").replace("[", "\\[")


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _meta_line(task: Task) -> str:
    """A small metadata line rendered above the task body."""
    parts: list[str] = [f"`{task.task_id}`", f"*{task.state}*"]
    if task.next_rank:
        parts.append(f"**next #{task.next_rank}**")
    if task.priority:
        parts.append(f"priority: {task.priority}")
    if task.labels:
        parts.append(" ".join(f"`{label}`" for label in task.labels))
    return " · ".join(parts) + "\n\n"


def _number_width(tasks: list[Task]) -> int:
    """Width of the widest id number present, or 0 when none of them have one."""
    return max((len(task.number or "") for task in tasks), default=0)


def _select(list_view: TaskListView, index: int) -> None:
    """Move the cursor and make sure the highlight follows.

    Assigning an index it already holds does not fire Textual's watcher, so a
    freshly rebuilt list would keep its cursor position with no bar drawn.
    """
    list_view.index = None
    list_view.index = index


def _index_of(names: list[str], target: str | None) -> int:
    if target is not None and target in names:
        return names.index(target)
    return 0


def _git_section(info: GitInfo | None, unreachable: bool = False) -> list[str]:
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
    ]
    notes = (_drift_note(info), _remote_note(info, unreachable))
    lines += [f"- {note}" for note in notes if note]
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


def _pull_request_section(pull: PullRequest | None) -> list[str]:
    """The PR this branch has open: what it says, and whether it can land."""
    if pull is None:
        return []
    lines = [
        "",
        f"## Pull request #{pull.number}",
        "",
        f"**{escape_markdown(pull.title)}**",
        "",
        f"- **Size** {pull.diffstat}",
        f"- **Checks** {escape_markdown(checks_summary(pull))}",
    ]
    review = pull.review_decision.replace("_", " ").lower()
    if review:
        lines.append(f"- **Review** {review}")
    if pull.blocking:
        lines.append(f"- **Merge** ⚠ **cannot merge** — {pull.blocking}")
    else:
        lines.append("- **Merge** press `M`")
    for warning in pull.warnings:
        lines.append(f"- ⚠ {escape_markdown(warning)}")
    lines += ["", f"[{pull.url}]({pull.url})"]

    # Comments come before the description: the description is the agent
    # explaining its own work, the comments are why you would say no.
    human = pull.human_comments
    if human:
        lines += ["", f"### {_plural(len(human), 'comment')}", ""]
        for entry in human[-5:]:
            first = entry.body.strip().splitlines()[0] if entry.body.strip() else ""
            when = f" · {_relative(entry.created_at)}" if entry.created_at else ""
            lines.append(
                f"- **{escape_markdown(entry.author)}**{when} — "
                f"{escape_markdown(_shorten(first, 160))}"
            )
        if len(human) > 5:
            lines.append(f"- *…{len(human) - 5} older*")
        lines += ["", "*`m` to reply.*"]

    if pull.body.strip():
        lines += ["", "### Description", "", pull.body.strip()]
    return lines


def _relative(timestamp: str) -> str:
    """`3h ago` from an ISO 8601 stamp, so you can see what is new."""
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return describe_age_phrase(moment)


def escape_markdown(text: str) -> str:
    """A PR title is arbitrary text; keep it from re-styling the pane."""
    return text.replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


def _drift_note(info: GitInfo) -> str:
    """How this checkout stands against its base, as a whole bullet line."""
    if info.base is None:
        # Standing on the base branch itself: the Remote line has the story.
        return "" if info.branch in ("main", "master") else (
            "**Base** no `main`/`master` branch to compare against"
        )
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


def _remote_note(info: GitInfo, unreachable: bool = False) -> str:
    """Where this branch stands against its remote, and how fresh that is."""
    if not info.has_remote:
        return "**Remote** none configured"
    checked = f" · checked {describe_age_phrase(info.fetched)}" if info.fetched else ""
    if unreachable:
        # A failed fetch still touches FETCH_HEAD, so its mtime would otherwise
        # claim the remote was reached moments ago.
        checked = " · **could not reach the remote** — counts may be stale"
    if info.upstream is None:
        return f"**Remote** never pushed — this branch exists only here{checked}"
    if info.upstream_gone:
        return (
            f"**Remote** `{info.upstream}` has been deleted — "
            f"the branch was published and then tidied up{checked}"
        )
    parts = [f"`{info.upstream}`"]
    if info.unpushed:
        parts.append(f"**{_plural(info.unpushed, 'commit')} to push**")
    if info.unpulled and info.tracks_own_branch:
        parts.append(f"**{_plural(info.unpulled, 'commit')} to pull** — press `u`")
    if not info.tracks_own_branch:
        parts.append(
            f"**tracks another branch** — {_plural(info.unpulled, 'commit')} "
            "behind it, but updating would move this branch onto it"
        )
    if info.in_step:
        parts.append("up to date")
    return f"**Remote** {' · '.join(parts)}{checked}"


def _dirty_note(info: GitInfo) -> str:
    if not info.dirty:
        return ""
    return f" · {_plural(info.dirty, 'uncommitted file')}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
