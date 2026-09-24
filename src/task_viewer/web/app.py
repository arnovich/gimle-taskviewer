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

import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
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
from .control import QUEUE_OPS, ControlError, ControlPlane, InvalidInput, RepoView
from .director import (
    FEED_LIMIT,
    SORTS,
    STALE_AFTER,
    agents,
    arrange,
    attention,
    ci,
    created_at,
    feed,
    main_health,
    next_pick,
    timeline,
    up_next,
)

# The dashboard shows this many events; /activity shows them all.
DASHBOARD_EVENTS = 12

_TEMPLATES = Path(__file__).parent / "templates"

# Nearly every task file opens with `# <title>`; the page already shows the
# title, so the body starts after it.
_LEADING_H1_RE = re.compile(r"\A\s*#[ \t]+[^\n]*\n")

# Agents' clocks and this machine's disagree by seconds, not hours. A stamp a
# few minutes ahead is "just now", not "in the future".
_SKEW = timedelta(minutes=5)

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
    templates.env.globals["next_pick"] = next_pick
    templates.env.globals["stale_after"] = f"{int(STALE_AFTER.total_seconds() // 3600)}h"

    def page(request: Request, name: str, views: list[RepoView], **context) -> HTMLResponse:
        """Render with the sidebar's data, which every page carries."""
        return templates.TemplateResponse(request, name, {"nav": _nav(views), **context})

    def load() -> list[RepoView]:
        control.refresh()
        return control.overview()

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        views = load()
        facts = [v.facts for v in views]
        return page(
            request,
            "index.html",
            views,
            attention=attention(facts),
            agents=agents(facts),
            picks=[(v.name, up_next(v.facts)) for v in views],
            events=feed(facts)[:DASHBOARD_EVENTS],
            ci=ci(facts, _branches(views)),
        )

    @app.get("/needs-you", response_class=HTMLResponse)
    def needs_you(request: Request) -> HTMLResponse:
        views = load()
        return page(request, "needs_you.html", views, attention=attention([v.facts for v in views]))

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, repo: str | None = None) -> HTMLResponse:
        views = load()
        chosen = [v for v in views if repo is None or v.name == repo]
        if repo is not None and not chosen:
            raise HTTPException(404, f"no repository called {repo!r}")
        return page(
            request,
            "activity.html",
            views,
            events=feed([v.facts for v in chosen], limit=FEED_LIMIT * 4),
            repo=repo,
        )

    @app.get("/r/{repo}", response_class=HTMLResponse)
    def repo(
        request: Request,
        repo: str,
        closed: bool = False,
        sort: str = "number",
        dir: str = "asc",
        q: str = "",
    ) -> HTMLResponse:
        views = load()
        view = _or_404(lambda: _find(views, repo))
        sort = sort if sort in SORTS else "number"
        descending = dir == "desc"
        shown = arrange(view.facts, view.tasks if closed else view.active, sort, descending, q[:200])
        return page(
            request,
            "repo.html",
            views,
            view=view,
            tasks=shown,
            closed=closed,
            sort=sort,
            descending=descending,
            q=q[:200],
            sorts=SORTS,
            created=created_at(view.facts),
            picks=up_next(view.facts),
            attention=attention([view.facts]),
            ci=ci([view.facts], _branches(views)),
            runs=(view.github.runs[:12] if view.github and view.github.runs_ok else []),
        )

    @app.get("/r/{repo}/t/{task_id}", response_class=HTMLResponse)
    def task(request: Request, repo: str, task_id: str) -> HTMLResponse:
        views = load()
        view = _or_404(lambda: _find(views, repo))
        found = _or_404(lambda: control.task(repo, task_id))
        return page(
            request,
            "task.html",
            views,
            view=view,
            task=found,
            pull=attention([view.facts]).pull_for(repo, found),
            body_html=_render_markdown(_without_title(strip_thread(found.body))),
            entries=found.conversation,
            events=timeline(view.facts, found),
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


def _branches(views: list[RepoView]) -> dict[str, str | None]:
    return {v.name: v.mirror.branch for v in views}


def _find(views: list[RepoView], name: str) -> RepoView:
    for view in views:
        if view.name == name:
            return view
    raise ControlError(f"no repository called {name!r}")


def _nav(views: list[RepoView]) -> dict:
    """What the sidebar shows on every page: the repos, and where you are needed."""
    rows = []
    total = 0
    checked = None
    for view in views:
        needs = attention([view.facts]).count
        total += needs
        detail = view.refresh.detail if view.refresh else ""
        if view.github is not None and not view.github.ok:
            detail = f"{detail} · " if detail else ""
            detail += f"GitHub: {view.github.error}"
        rows.append({
            "name": view.name,
            "counts": view.counts,
            "needs": needs,
            "error": view.error,
            "detail": detail,
            "ci": main_health(view.facts, view.mirror.branch),
        })
        if view.refresh and view.refresh.at and (checked is None or view.refresh.at > checked):
            checked = view.refresh.at
    return {"repos": rows, "needs": total, "checked": checked}


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


def _without_title(body: str) -> str:
    return _LEADING_H1_RE.sub("", body, count=1)


def _ago(value) -> str:
    """``3h ago`` from a datetime, a date or an ISO stamp; the raw text if it is none of those.

    YAML hands over a datetime (aware or naive, depending on whether the
    author wrote a ``Z``) or a date; an author may also have quoted the stamp.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < value <= now + _SKEW:
            value = now
        return describe_age_phrase(value, now)
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
        return _ago(parsed)
    return "unknown"
