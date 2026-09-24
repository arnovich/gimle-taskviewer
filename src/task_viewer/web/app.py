"""The pages: a dashboard, one page per repo, one page per task.

Server-rendered HTML and plain forms — no build step, no client state. Every
GET refreshes the mirrors lazily first, so what you see is the remote as of
at most ``max_age`` seconds ago, and the page says when it last looked.

Two things are guarded even on localhost, because a browser on localhost is
still a browser that visits other sites: the ``Host`` header must name this
machine (a rebinding page cannot read the dashboard), and a POST must come
from this origin (a page elsewhere cannot push commits as the owner). Task
markdown is rendered with raw HTML off, since agents write those files.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..conversation import KINDS, strip_thread
from ..discovery import Task
from ..git_info import describe_age_phrase
from .control import QUEUE_OPS, ControlError, ControlPlane, InvalidInput

_TEMPLATES = Path(__file__).parent / "templates"

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

_markdown = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")

# Nothing runs, nothing is fetched from elsewhere, and nothing frames us.
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def create_app(control: ControlPlane, allowed_hosts: Iterable[str] = LOCAL_HOSTS) -> FastAPI:
    app = FastAPI(title="tv control plane", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def same_origin_and_headers(request: Request, call_next):
        if request.method == "POST":
            refusal = _cross_site(request)
            if refusal:
                return PlainTextResponse(refusal, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    templates = Jinja2Templates(directory=str(_TEMPLATES))
    templates.env.filters["ago"] = _ago
    templates.env.filters["markdown"] = _render_markdown
    templates.env.globals["owner"] = control.owner
    templates.env.globals["kinds"] = KINDS
    templates.env.globals["repo_url"] = _repo_url
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
        return page(
            request,
            "repo.html",
            view=view,
            tasks=view.tasks if closed else view.active,
            closed=closed,
        )

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


def _cross_site(request: Request) -> str:
    """Why a POST is refused, or ``""`` when it plainly came from this origin.

    Browsers say where a request came from twice: ``Sec-Fetch-Site`` (modern)
    and ``Origin`` (older). Either one disagreeing is enough to refuse; a
    request that sends neither — curl, a form on this very page in an old
    browser — is allowed, because the Host check above already limits who can
    reach the app at all.
    """
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return "cross-site request refused"
    origin = request.headers.get("origin")
    if origin and origin != _origin_of(str(request.base_url)):
        return "cross-origin request refused"
    return ""


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _or_404(load):
    try:
        return load()
    except ControlError as error:
        raise HTTPException(404, str(error)) from error


def _or_error(act):
    """A write that failed: the request was wrong (400) or the repo refused (409)."""
    try:
        return act()
    except InvalidInput as error:
        raise HTTPException(400, str(error)) from error
    except ControlError as error:
        raise HTTPException(409, str(error)) from error


def _back(request: Request) -> RedirectResponse:
    """Return to the page the form was on, if it was one of ours."""
    referer = request.headers.get("referer", "")
    ours = _origin_of(str(request.base_url))
    target = referer if _origin_of(referer) == ours else "/"
    return RedirectResponse(target, status_code=303)


def _repo_url(repo: str) -> str:
    return f"/r/{quote(repo, safe='')}"


def _task_url(repo: str, task_id: str) -> str:
    return f"{_repo_url(repo)}/t/{quote(task_id, safe='')}"


def _github_url(url: str, task: Task, branch: str | None) -> str | None:
    """The file on GitHub, when the remote is one."""
    base = url.strip().rstrip("/").removesuffix(".git")
    if base.startswith("git@github.com:"):
        base = "https://github.com/" + base[len("git@github.com:"):]
    if not base.startswith("https://github.com/"):
        return None
    rel = task.path.relative_to(task.path.parent.parent.parent)
    kind = "tree" if task.path.is_dir() else "blob"
    return f"{base}/{kind}/{quote(branch or 'main')}/{quote(rel.as_posix())}"


def _render_markdown(text: str) -> str:
    return _markdown.render(text)


def _ago(value) -> str:
    """``3h ago`` from a datetime, a date or an ISO stamp; the raw text if it is none of those.

    YAML hands over a datetime (aware or naive, depending on whether the
    author wrote a ``Z``) or a date; an author may also have quoted the stamp.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return describe_age_phrase(value)
    if isinstance(value, date):
        return describe_age_phrase(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))
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
