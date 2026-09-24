"""Tests for the control plane pages, against real repos behind real mirrors."""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from task_viewer.discovery import Task
from task_viewer.mirror import Mirror
from task_viewer.web.app import _ago, _github_url, create_app
from task_viewer.web.config import ConfigError, load_config, resolve_owner
from task_viewer.web.control import ControlPlane

from helpers import clone_repo, git, init_repo

QUESTION = (
    "\n## Conversation\n\n"
    "### question · claude/abc · 2026-09-24T10:02:00Z\n\n"
    "Which GPU is the reference?\n"
)


def _task(title: str, state: str = "open", extra: str = "", body: str = "") -> str:
    return (
        f"---\ntitle: {title}\nstate: {state}\npriority: medium\nlabels: [x]\n{extra}---\n\n"
        f"# {title}\n\n## Context\n\nWhy.\n{body}"
    )


def _origin(tmp_path: Path, name: str, files: dict[str, str], history=(), branches=()) -> Path:
    """A bare 'GitHub' seeded with ``files``, then ``history`` commits, then branches.

    ``history`` is a list of (subject, {path: text}) applied in order, the way
    grind's own metadata commits land; ``branches`` is a list of names pushed
    from the tip, the way a task branch stays on the remote until its PR merges.
    """
    bare = tmp_path / f"{name}.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    seed = init_repo(tmp_path / f"{name}-seed")
    git(seed, "remote", "add", "origin", str(bare))

    def commit(subject: str, changes: dict[str, str | None]) -> None:
        for rel, text in changes.items():
            path = seed / rel
            if text is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", subject)

    commit("Seed tasks", files)
    for subject, changes in history:
        commit(subject, changes)
    git(seed, "push", "-q", "origin", "main")
    for branch in branches:
        git(seed, "push", "-q", "origin", f"main:refs/heads/{branch}")
    return bare


@pytest.fixture
def world(tmp_path: Path):
    alpha = _origin(
        tmp_path,
        "alpha",
        {
            "tasks/open/001-asked.md": _task("Asked", body=QUESTION),
            "tasks/open/002-queued.md": _task("Queued", extra="next: 1\ndepends_on: [001-asked]\n"),
            "tasks/open/003-busy.md": _task("Busy", extra="next: 2\n"),
            "tasks/closed/004-done.md": _task("Done", "closed", extra="next: 5\n"),
            "tasks/open/005-stuck.md": _task(
                "Stuck", extra="next: 3\n",
                body="\n## Attempts\n\n- 2026-09-20 — no such module\n- 2026-09-21 — tests hang\n",
            ),
            "tasks/open/006-twin-a.md": _task("Twin A"),
            "tasks/open/006-twin-b.md": _task("Twin B"),
            "tasks/open/007-free.md": _task("Free", extra="next: 4\n"),
            # Hand-edited stamps: naive, and a bare date. Both must render.
            "tasks/ongoing/008-naive.md": _task(
                "Naive", "ongoing", "claimed_by: claude/n\nclaimed_at: 2026-09-24T09:00:00\n"
            ),
            "tasks/ongoing/009-dated.md": _task(
                "Dated", "ongoing", "claimed_by: claude/d\nclaimed_at: 2026-09-24\n"
            ),
            "tasks/open/010-dir/description.md": _task("Dir task", body=QUESTION),
            "tasks/open/010-dir/plan.md": "The plan.\n",
        },
        history=[
            ("task 003: claim", {
                "tasks/open/003-busy.md": None,
                "tasks/ongoing/003-busy.md": _task(
                    "Busy", "ongoing",
                    "next: 2\nclaimed_by: claude/xyz\nclaimed_at: 2026-09-24T09:00:00Z\nbranch: task/003_busy\n",
                ),
            }),
            ("task 003: plan", {
                "tasks/ongoing/003-busy.md": _task(
                    "Busy", "ongoing",
                    "next: 2\nclaimed_by: claude/xyz\nclaimed_at: 2026-09-24T09:00:00Z\nbranch: task/003_busy\n",
                    body="\n## Plan\n\nDo it.\n\n## Conversation\n\n### note · claude/xyz · 2026-09-24T09:30:00Z\n\nPlan written, reviews next.\n",
                ),
            }),
            ("Merge pull request #7 from arnovich/task/004_done", {"README.md": "merged\n"}),
        ],
        branches=["task/003_busy", "task/004_done"],
    )
    beta = _origin(tmp_path, "beta", {
        "tasks/open/001-mine.md": _task(
            "Mine", body="\n## Conversation\n\n### question · erikarne · 2026-09-24T11:00:00Z\n\nStatus?\n"
        ),
        "tasks/closed/002-late.md": _task(
            "Late", "closed",
            body="\n## Conversation\n\n### question · erikarne · 2026-09-24T12:00:00Z\n\nWhy closed?\n",
        ),
    })
    mirrors = [
        Mirror(str(alpha), tmp_path / "data" / "alpha"),
        Mirror(str(beta), tmp_path / "data" / "beta"),
    ]
    control = ControlPlane(mirrors, owner="erikarne", max_age=3600)
    app = create_app(control, allowed_hosts=["testserver"])
    client = TestClient(app, follow_redirects=False)
    return {"alpha": alpha, "beta": beta, "client": client, "control": control, "tmp": tmp_path}


def _show(origin: Path, rel: str) -> str:
    return subprocess.run(
        ["git", "-C", str(origin), "show", f"main:{rel}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _subjects(origin: Path) -> list[str]:
    return subprocess.run(
        ["git", "-C", str(origin), "log", "--format=%s", "main"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()


def _section(page: str, anchor: str) -> str:
    """The HTML between the heading with this id and the next heading."""
    marker = f'id="{anchor}"'
    assert marker in page, f"no section {anchor}"
    return page.split(marker, 1)[1].split("<h2", 1)[0]


def test_the_dashboard_says_what_is_your_move(world) -> None:
    page = world["client"].get("/").text
    needs_you = _section(page, "needs-you")
    # 2 questions + 1 branch ready to merge + 1 given up + 1 ambiguous number.
    assert 'class="number">5<' in needs_you
    assert "2 questions, 1 branch ready to merge, 1 task given up on, 1 ambiguous number" in needs_you
    assert ">Asked<" in needs_you and ">Dir task<" in needs_you and "claude/abc" in needs_you
    assert ">Mine<" not in needs_you  # the owner asked that one; it is on /needs-you
    # The other buckets are folded, but present.
    assert "<details>" in needs_you
    assert ">Done<" in needs_you and "task/004_done" in needs_you
    assert ">Stuck<" in needs_you
    assert ">Twin A<" in needs_you and ">Twin B<" in needs_you


def test_the_needs_you_page_has_every_bucket_in_full(world) -> None:
    page = world["client"].get("/needs-you").text
    assert ">Asked<" in _section(page, "questions")
    assert ">Done<" in _section(page, "ready")
    assert ">Stuck<" in _section(page, "gave-up")
    assert ">Twin A<" in _section(page, "ambiguous")
    waiting_on_agent = _section(page, "needs-agent")
    assert ">Mine<" in waiting_on_agent and ">Late<" in waiting_on_agent  # closed tasks count


def test_the_sidebar_lists_repos_with_what_needs_you(world) -> None:
    page = world["client"].get("/r/beta").text
    side = page.split('<nav class="side">')[1].split("</nav>")[0]
    assert 'href="/r/alpha"' in side and 'href="/r/beta"' in side
    assert 'class="item repo here"' in side and ">beta<" in side
    alpha_row = side.split('href="/r/alpha"')[1].split("</a>")[0]
    assert '<span class="needs">5</span>' in alpha_row
    assert 'href="/needs-you">Needs you <span class="needs">5</span>' in side
    assert "checked just now" in side


def test_the_dashboard_shows_who_is_running_and_when_they_were_last_seen(world) -> None:
    page = world["client"].get("/").text
    running = _section(page, "in-progress")
    assert "claude/xyz" in running and ">Busy<" in running
    assert "task/003_busy" in running  # the branch on the remote is the heartbeat
    assert "Plan written, reviews next." in running  # the last note is the status line
    assert "claude/n" in running and ">Naive<" in running
    assert "claude/d" in running and ">Dated<" in running


def test_the_dashboard_names_the_next_pick_and_the_repo_page_explains_the_rest(world) -> None:
    client = world["client"]
    up_next = _section(client.get("/").text, "up-next")
    alpha = up_next.split(">alpha<")[1].split("</li>")[0]
    assert ">Free<" in alpha and "+3 ranked" in alpha
    assert "nothing ranked" in up_next.split(">beta<")[1]

    queue = _section(client.get("/r/alpha").text, "up-next")
    rows = [line for line in queue.splitlines() if "<li" in line]
    assert [r.split("</a>")[0].rsplit(">", 1)[1] for r in rows] == ["Queued", "Busy", "Stuck", "Free"]
    assert "blocked by 001-asked" in rows[0]
    assert "claimed by claude/xyz" in rows[1]
    assert "given up after 2 attempts" in rows[2]
    assert 'class="next"' in rows[3] and ">next<" in rows[3]


def test_the_dashboard_and_the_activity_page_list_what_happened(world) -> None:
    client = world["client"]
    activity = _section(client.get("/").text, "activity")
    assert ">claimed<" in activity and ">planned<" in activity
    assert ">merged<" in activity and "#7" in activity and ">Done<" in activity
    assert ">note<" in activity and "Plan written, reviews next." in activity
    # The note's own commit ("task 003: plan") is one event with the entry, not two.
    assert activity.count("Plan written") == 1
    assert ">question<" in activity and "Which GPU" in activity

    everything = client.get("/activity").text
    assert "Which GPU" in everything
    assert "Status?" in everything  # beta's thread
    only_beta = client.get("/activity?repo=beta").text
    assert "Status?" in only_beta and "Which GPU" not in only_beta
    assert client.get("/activity?repo=nope").status_code == 404


def test_the_repo_page_lists_active_tasks_and_can_show_closed(world) -> None:
    client = world["client"]
    page = client.get("/r/alpha").text
    assert "Asked" in page and "Busy" in page and ">Done<" not in page
    assert 'class="ask"' in page  # the waiting marker
    assert "Done" in client.get("/r/alpha?closed=1").text
    assert client.get("/r/nope").status_code == 404
    assert client.get("/r/alpha/t/999-missing").status_code == 404


def test_the_task_page_renders_the_body_and_the_thread(world) -> None:
    page = world["client"].get("/r/alpha/t/001-asked").text
    assert "<h2>Context</h2>" in page
    assert page.count("Asked</h1>") == 1  # the file's own `# Asked` is not repeated
    assert "Which GPU is the reference?" in page
    assert 'class="entry question"' in page
    assert "Your answer is needed." in page
    # The thread is rendered as entries, not as part of the body.
    assert page.count("Which GPU") == 1
    assert 'value="answer" checked' in page


def test_the_task_page_tells_the_tasks_history(world) -> None:
    page = world["client"].get("/r/alpha/t/003-busy").text
    history = page.split('id="timeline"')[1].split("</form>")[0]
    assert ">claimed<" in history and ">planned<" in history and "Plan written" in history
    assert history.index(">claimed<") < history.index(">planned<")  # log order, oldest first
    assert 'class="event"' in history and 'class="entry note"' in history


def test_task_markdown_cannot_inject_html(world, tmp_path: Path) -> None:
    other = clone_repo(tmp_path, world["alpha"], tmp_path / "other")
    path = other / "tasks" / "open" / "005-evil.md"
    path.write_text(_task("Evil", body="\n<script>alert(1)</script>\n"))
    git(other, "add", "tasks"); git(other, "commit", "-m", "evil"); git(other, "push", "-q", "origin", "main")
    world["control"].refresh(force=True)
    response = world["client"].get("/r/alpha/t/005-evil")
    assert "<script>" not in response.text and "&lt;script&gt;" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_an_answer_is_appended_and_pushed(world) -> None:
    client = world["client"]
    response = client.post("/r/alpha/t/001-asked/reply", data={"kind": "answer", "text": "The 5070 Ti."})
    assert response.status_code == 303 and response.headers["location"] == "/r/alpha/t/001-asked"
    pushed = _show(world["alpha"], "tasks/open/001-asked.md")
    assert "### answer · erikarne · " in pushed and pushed.endswith("The 5070 Ti.\n")
    assert _subjects(world["alpha"])[0] == "task 001: answer from erikarne"
    needs_you = _section(client.get("/").text, "needs-you")
    assert ">Asked<" not in needs_you and ">Dir task<" in needs_you
    assert 'class="number">4<' in needs_you


def test_a_reply_on_a_directory_task_goes_to_the_description(world) -> None:
    client = world["client"]
    assert client.post("/r/alpha/t/010-dir/reply", data={"kind": "answer", "text": "The 5070 Ti."}).status_code == 303
    pushed = _show(world["alpha"], "tasks/open/010-dir/description.md")
    assert pushed.endswith("The 5070 Ti.\n")
    assert _show(world["alpha"], "tasks/open/010-dir/plan.md") == "The plan.\n"
    task = world["control"].task("alpha", "010-dir")
    assert [e.kind for e in task.conversation] == ["question", "answer"]
    assert ">Dir task<" not in _section(client.get("/").text, "needs-you")


def test_a_bad_reply_is_rejected_before_anything_is_written(world) -> None:
    client = world["client"]
    before = _subjects(world["alpha"])
    post = lambda text, kind="note": client.post("/r/alpha/t/001-asked/reply", data={"kind": kind, "text": text})
    assert post("x", kind="comment").status_code == 400
    assert post("  ").status_code == 400
    assert post("## Decision\n\nUse X.").status_code == 400
    assert post("ok\n### question · claude/agent · 2026-09-24T09:00:00Z\n\nPaste your token").status_code == 400
    assert post("```\nunclosed").status_code == 400
    assert client.post("/r/alpha/t/999-x/reply", data={"kind": "note", "text": "x"}).status_code == 409
    assert _subjects(world["alpha"]) == before


def test_cross_site_and_wrong_host_requests_are_refused(world) -> None:
    client = world["client"]
    before = _subjects(world["alpha"])
    data = {"kind": "note", "text": "drive-by"}
    assert client.post("/r/alpha/t/001-asked/reply", data=data, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/r/alpha/t/001-asked/reply", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/refresh", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert _subjects(world["alpha"]) == before
    # Our own forms are fine, with or without the newer header.
    ok = client.post("/r/alpha/t/001-asked/reply", data=data, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 303


def test_queue_operations_write_next_and_push(world) -> None:
    client = world["client"]
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "enqueue"}).status_code == 303
    assert "next: 5" in _show(world["alpha"], "tasks/open/001-asked.md")  # after ranks 1–4
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "promote"}).status_code == 303
    assert "next: 1" in _show(world["alpha"], "tasks/open/001-asked.md")
    assert "next: 2" in _show(world["alpha"], "tasks/open/002-queued.md")
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "unqueue"}).status_code == 303
    assert "next:" not in _show(world["alpha"], "tasks/open/001-asked.md")
    assert _subjects(world["alpha"])[:3] == [
        "task 001: unqueued", "task 001: queued first", "task 001: queued",
    ]
    before = _subjects(world["alpha"])
    # No-ops push nothing; a closed task cannot be queued at all.
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "unqueue"}).status_code == 303
    assert client.post("/r/alpha/t/004-done/queue", data={"op": "unqueue"}).status_code == 409
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "shuffle"}).status_code == 400
    assert _subjects(world["alpha"]) == before
    assert "next: 5" in _show(world["alpha"], "tasks/closed/004-done.md")


def test_refresh_picks_up_what_an_agent_pushed(world, tmp_path: Path) -> None:
    client = world["client"]
    client.get("/")  # mirrors are now fresh for an hour
    other = clone_repo(tmp_path, world["alpha"], tmp_path / "other")
    (other / "tasks" / "open" / "006-new.md").write_text(_task("Brand new"))
    git(other, "add", "tasks"); git(other, "commit", "-m", "new"); git(other, "push", "-q", "origin", "main")
    assert "Brand new" not in client.get("/r/alpha").text
    response = client.post("/refresh", headers={"referer": "http://testserver/r/alpha"})
    assert response.status_code == 303 and response.headers["location"] == "http://testserver/r/alpha"
    assert "Brand new" in client.get("/r/alpha").text
    elsewhere = client.post("/refresh", headers={"referer": "http://evil.example/r/alpha"})
    assert elsewhere.headers["location"] == "/"


def test_an_unreachable_repo_is_shown_not_fatal(world, tmp_path: Path) -> None:
    control = world["control"]
    control.refresh(force=True)  # clones; the remote can only vanish afterwards
    git(control.mirror("beta").root, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    control.refresh(force=True)
    page = world["client"].get("/r/beta").text
    assert "could not reach the remote" in page
    assert "Mine" in page  # the last known state is still served
    assert 'class="warn" title="could not reach the remote"' in page  # the sidebar marks it


def test_a_repo_that_cannot_be_cloned_is_shown_and_refuses_writes(tmp_path: Path) -> None:
    mirror = Mirror("https://127.0.0.1:1/nobody/gamma.git", tmp_path / "data" / "gamma")
    control = ControlPlane([mirror], owner="erikarne", max_age=3600)
    client = TestClient(create_app(control, allowed_hosts=["testserver"]), follow_redirects=False)
    page = client.get("/").text
    assert "gamma" in page and "clone failed" in page
    assert client.post("/r/gamma/t/001-x/reply", data={"kind": "note", "text": "x"}).status_code == 409


def test_a_symlinked_task_is_not_followed(world, tmp_path: Path) -> None:
    other = clone_repo(tmp_path, world["alpha"], tmp_path / "other")
    (other / "tasks" / "open" / "009-link.md").symlink_to(tmp_path / "beta.git" / "HEAD")
    git(other, "add", "tasks"); git(other, "commit", "-m", "link"); git(other, "push", "-q", "origin", "main")
    world["control"].refresh(force=True)
    page = world["client"].get("/r/alpha/t/009-link").text
    assert "ref: refs/heads/main" not in page  # the target's content never appears


def test_github_links_and_ages() -> None:
    task = Task("001-x", "X", "open", Path("/data/repo/tasks/open/001-x.md"), "")
    assert _github_url("https://github.com/a/b.git/", task, "main") == "https://github.com/a/b/blob/main/tasks/open/001-x.md"
    assert _github_url("git@github.com:a/b.git", task, None) == "https://github.com/a/b/blob/main/tasks/open/001-x.md"
    assert _github_url("https://example.com/a/b.git", task, "main") is None
    now = datetime.now(timezone.utc)
    assert _ago(now) == "just now"
    assert _ago(now.replace(tzinfo=None)) == "just now"
    assert _ago(now + timedelta(minutes=3)) == "just now"  # clock skew, not prophecy
    assert _ago(now + timedelta(hours=3)) == "in the future"
    assert _ago(date(2020, 1, 1)).endswith("ago")
    assert _ago("2020-01-01T10:00:00Z").endswith("ago")
    assert _ago("yesterday-ish") == "yesterday-ish"
    assert _ago(None) == "unknown"


def test_config_is_read_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert load_config(None).repos == []  # no default file yet: defaults, no error
    path = tmp_path / "web.toml"
    path.write_text(
        'owner = "me"\nrepos = ["https://github.com/x/y.git"]\nmax_age = 5\nport = 9999\n'
        'data_dir = "~/somewhere"\n'
    )
    config = load_config(path)
    assert config.owner == "me" and config.repos == ["https://github.com/x/y.git"]
    assert config.max_age == 5.0 and config.port == 9999
    assert config.data_dir == Path("~/somewhere").expanduser()
    default = tmp_path / "xdg" / "tv" / "web.toml"
    default.parent.mkdir(parents=True)
    default.write_text('repos = ["https://github.com/x/z.git"]\n')
    assert load_config(None).repos == ["https://github.com/x/z.git"]
    for bad in ("repos = 'not a list'\n", "port = 'eighty'\n", "max_age = 'soon'\n", "not toml\n"):
        path.write_text(bad)
        with pytest.raises(ConfigError):
            load_config(path)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")  # named explicitly, so it must exist


def test_the_owner_handle_is_one_safe_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # not inside any git checkout
    global_config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    assert resolve_owner("  erikarne ") == "erikarne"
    assert resolve_owner("Erik · Arne") == "Erik-Arne"
    global_config.write_text("[user]\n\tname = Erik Arne\n")
    assert resolve_owner("") == "Erik-Arne"
    global_config.write_text("[user]\n\tname = ·|·\n")
    assert resolve_owner("") == "owner"
    global_config.write_text("")
    assert resolve_owner("") == "owner"
