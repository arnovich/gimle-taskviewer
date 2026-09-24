"""The pages: a dashboard, one page per repo, one page per task.

Server-rendered HTML and plain forms — no build step, no client state. Every
GET refreshes the mirrors lazily first, so what you see is the remote as of
at most ``max_age`` seconds ago, and the page says when it last looked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ..conversation import KINDS, strip_thread
from ..discovery import Task
from ..git_info import describe_age_phrase
from .control import QUEUE_OPS, ControlError, ControlPlane

_TEMPLATES = Path(__file__).parent / "templates"

# Raw HTML off: a task file is written by agents, and a page that renders
# their markdown must not also run their script tags.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def create_app(control: ControlPlane) -> FastAPI:
    app = FastAPI(title="tv control plane", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    templates.env.filters["ago"] = _ago
    templates.env.filters["markdown"] = _render_markdown
    templates.env.filters["stamp"] = _stamp
    templates.env.globals["owner"] = control.owner
    templates.env.globals["kinds"] = KINDS
    templates.env.globals["task_url"] = _task_url
    templates.env.globals["github_url"] = _github_url

    def page(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        control.refresh()
        views = control.overview()
        waiting = control.waiting(views)
        return page(
            request,
            "index.html",
            views=views,
            needs_owner=[w for w in waiting if w.on_owner],
            needs_agent=[w for w in waiting if not w.on_owner],
            ongoing=[(v.name, t) for v in views for t in v.ongoing],
        )

    @app.get("/r/{repo}", response_class=HTMLResponse)
    def repo(request: Request, repo: str, closed: bool = False) -> HTMLResponse:
        control.refresh()
        view = _or_404(lambda: control.repo(repo))
        tasks = view.tasks if closed else [t for t in view.tasks if t.state != "closed"]
        return page(request, "repo.html", view=view, tasks=tasks, closed=closed)

    @app.get("/r/{repo}/t/{task_id}", response_class=HTMLResponse)
    def task(request: Request, repo: str, task_id: str) -> HTMLResponse:
        control.refresh()
        view = _or_404(lambda: control.repo(repo))
        found = _or_404(lambda: control.task(repo, task_id))
        return page(
            request,
            "task.html",
            view=view,
            task=found,
            body_html=_render_markdown(strip_thread(found.body)),
            entries=found.conversation,
            question=found.open_question,
            queue_ops=QUEUE_OPS,
        )

    @app.post("/refresh")
    def refresh(request: Request) -> RedirectResponse:
        control.refresh(force=True)
        return _back(request)

    @app.post("/r/{repo}/t/{task_id}/reply")
    def reply(
        repo: str, task_id: str, kind: str = Form(...), text: str = Form(...)
    ) -> RedirectResponse:
        if kind not in KINDS:
            raise HTTPException(400, f"unknown entry kind {kind!r}")
        if not text.strip():
            raise HTTPException(400, "an entry needs some text")
        _or_error(lambda: control.reply(repo, task_id, kind, text))
        return RedirectResponse(_task_url(repo, task_id), status_code=303)

    @app.post("/r/{repo}/t/{task_id}/queue")
    def queue(repo: str, task_id: str, op: str = Form(...)) -> RedirectResponse:
        if op not in QUEUE_OPS:
            raise HTTPException(400, f"unknown queue operation {op!r}")
        _or_error(lambda: control.queue(repo, task_id, op))
        return RedirectResponse(_task_url(repo, task_id), status_code=303)

    return app


def _or_404(load):
    try:
        return load()
    except ControlError as error:
        raise HTTPException(404, str(error)) from error


def _or_error(act):
    """A write that failed is the remote's fault or a conflict — say which."""
    try:
        return act()
    except ControlError as error:
        raise HTTPException(409, str(error)) from error


def _back(request: Request) -> RedirectResponse:
    referer = request.headers.get("referer", "")
    target = referer if referer.startswith(str(request.base_url)) else "/"
    return RedirectResponse(target, status_code=303)


def _task_url(repo: str, task_id: str) -> str:
    return f"/r/{quote(repo, safe='')}/t/{quote(task_id, safe='')}"


def _github_url(url: str, task: Task, branch: str | None) -> str | None:
    """The file on GitHub, when the remote is one."""
    base = url.removesuffix(".git").removesuffix("/")
    if base.startswith("git@github.com:"):
        base = "https://github.com/" + base[len("git@github.com:"):]
    if not base.startswith("https://github.com/"):
        return None
    tasks_dir = task.path.parent.parent
    rel = task.path.relative_to(tasks_dir.parent)
    kind = "tree" if task.path.is_dir() else "blob"
    return f"{base}/{kind}/{branch or 'main'}/{rel.as_posix()}"


def _render_markdown(text: str) -> str:
    return _markdown.render(text)


def _ago(value) -> str:
    """``3h ago`` from a datetime or an ISO stamp; the raw text if it is neither."""
    if isinstance(value, datetime):
        return describe_age_phrase(value)
    if isinstance(value, str):
        stamp = value.strip().replace(" ", "T")
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return describe_age_phrase(parsed)
    return "unknown"


def _stamp(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value else "never"
