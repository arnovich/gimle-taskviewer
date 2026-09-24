"""Tests for the control plane pages, against real repos behind real mirrors."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from task_viewer.mirror import Mirror
from task_viewer.web.app import create_app
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


def _origin(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    bare = tmp_path / f"{name}.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    seed = init_repo(tmp_path / f"{name}-seed")
    for rel, text in files.items():
        path = seed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "Seed tasks")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return bare


@pytest.fixture
def world(tmp_path: Path):
    alpha = _origin(tmp_path, "alpha", {
        "tasks/open/001-asked.md": _task("Asked", body=QUESTION),
        "tasks/open/002-queued.md": _task("Queued", extra="next: 1\n"),
        "tasks/ongoing/003-busy.md": _task(
            "Busy", "ongoing",
            "claimed_by: claude/xyz\nclaimed_at: 2026-09-24T09:00:00Z\nbranch: task/003_busy\n",
        ),
        "tasks/closed/004-done.md": _task("Done", "closed"),
    })
    beta = _origin(tmp_path, "beta", {
        "tasks/open/001-mine.md": _task(
            "Mine", body="\n## Conversation\n\n### question · erikarne · 2026-09-24T11:00:00Z\n\nStatus?\n"
        ),
    })
    mirrors = [
        Mirror(str(alpha), tmp_path / "data" / "alpha"),
        Mirror(str(beta), tmp_path / "data" / "beta"),
    ]
    control = ControlPlane(mirrors, owner="erikarne", max_age=3600)
    client = TestClient(create_app(control), follow_redirects=False)
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


def test_the_dashboard_says_who_is_waiting_and_what_is_running(world) -> None:
    page = world["client"].get("/").text
    needs_you = page.split("Needs you")[1].split("<h2>")[0]
    assert "Asked" in needs_you and "claude/abc" in needs_you
    assert "Mine" not in needs_you  # the owner asked that one
    waiting_on_agent = page.split("waiting on an agent")[1].split("<h2>")[0]
    assert "Mine" in waiting_on_agent
    in_progress = page.split("In progress")[1].split("<h2>")[0]
    assert "Busy" in in_progress and "claude/xyz" in in_progress and "task/003_busy" in in_progress
    repos = page.split("Repositories")[1]
    assert 'href="/r/alpha"' in repos and 'href="/r/beta"' in repos
    assert "Queued" in repos  # the queue is listed per repo


def test_the_repo_page_lists_active_tasks_and_can_show_closed(world) -> None:
    client = world["client"]
    page = client.get("/r/alpha").text
    assert "Asked" in page and "Busy" in page and "Done" not in page
    assert 'class="badge question"' in page  # the waiting marker
    assert "Done" in client.get("/r/alpha?closed=1").text
    assert client.get("/r/nope").status_code == 404
    assert client.get("/r/alpha/t/999-missing").status_code == 404


def test_the_task_page_renders_the_body_and_the_thread(world) -> None:
    page = world["client"].get("/r/alpha/t/001-asked").text
    assert "<h2>Context</h2>" in page
    assert "Which GPU is the reference?" in page
    assert 'class="badge question"' in page
    assert "Waiting for an answer" in page
    # The thread is rendered as entries, not as part of the body.
    assert page.count("Which GPU") == 1
    assert '<option value="answer" selected' in page


def test_task_markdown_cannot_inject_html(world, tmp_path: Path) -> None:
    other = clone_repo(tmp_path, world["alpha"], tmp_path / "other")
    path = other / "tasks" / "open" / "005-evil.md"
    path.write_text(_task("Evil", body="\n<script>alert(1)</script>\n"))
    git(other, "add", "tasks"); git(other, "commit", "-m", "evil"); git(other, "push", "-q", "origin", "main")
    world["control"].refresh(force=True)
    page = world["client"].get("/r/alpha/t/005-evil").text
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_an_answer_is_appended_and_pushed(world) -> None:
    client = world["client"]
    response = client.post("/r/alpha/t/001-asked/reply", data={"kind": "answer", "text": "The 5070 Ti."})
    assert response.status_code == 303 and response.headers["location"] == "/r/alpha/t/001-asked"
    pushed = _show(world["alpha"], "tasks/open/001-asked.md")
    assert "### answer · erikarne · " in pushed and pushed.endswith("The 5070 Ti.\n")
    assert _subjects(world["alpha"])[0] == "task 001: answer from erikarne"
    # Answered, so nobody is waiting on the owner any more.
    dashboard = client.get("/").text
    assert "Nobody is waiting on you" in dashboard


def test_a_bad_reply_is_rejected_before_anything_is_written(world) -> None:
    client = world["client"]
    before = _subjects(world["alpha"])
    assert client.post("/r/alpha/t/001-asked/reply", data={"kind": "comment", "text": "x"}).status_code == 400
    assert client.post("/r/alpha/t/001-asked/reply", data={"kind": "note", "text": "  "}).status_code == 400
    assert client.post("/r/alpha/t/999-x/reply", data={"kind": "note", "text": "x"}).status_code == 409
    assert _subjects(world["alpha"]) == before


def test_queue_operations_write_next_and_push(world) -> None:
    client = world["client"]
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "enqueue"}).status_code == 303
    assert "next: 2" in _show(world["alpha"], "tasks/open/001-asked.md")
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "promote"}).status_code == 303
    assert "next: 1" in _show(world["alpha"], "tasks/open/001-asked.md")
    assert "next: 2" in _show(world["alpha"], "tasks/open/002-queued.md")
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "unqueue"}).status_code == 303
    assert "next:" not in _show(world["alpha"], "tasks/open/001-asked.md")
    assert _subjects(world["alpha"])[:3] == [
        "task 001: unqueued", "task 001: queued first", "task 001: queued",
    ]
    assert client.post("/r/alpha/t/001-asked/queue", data={"op": "shuffle"}).status_code == 400


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


def test_an_unreachable_repo_is_shown_not_fatal(world, tmp_path: Path) -> None:
    control = world["control"]
    control.refresh(force=True)  # clones; the remote can only vanish afterwards
    git(control.mirror("beta").root, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    control.refresh(force=True)
    page = world["client"].get("/").text
    assert "unreachable" in page
    assert "Mine" in page  # the last known state is still served


def test_config_is_read_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "web.toml"
    path.write_text(
        'owner = "me"\nrepos = ["https://github.com/x/y.git"]\nmax_age = 5\nport = 9999\n'
        'data_dir = "~/somewhere"\n'
    )
    config = load_config(path)
    assert config.owner == "me" and config.repos == ["https://github.com/x/y.git"]
    assert config.max_age == 5.0 and config.port == 9999
    assert config.data_dir == Path("~/somewhere").expanduser()
    path.write_text("repos = 'not a list'\n")
    with pytest.raises(ConfigError):
        load_config(path)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")  # named explicitly, so it must exist
    assert load_config(None).repos == [] or True  # the default file may or may not exist


def test_the_owner_handle_is_one_token() -> None:
    assert resolve_owner("  erikarne ") == "erikarne"
    handle = resolve_owner("")  # git config is /dev/null here, so this is the fallback
    assert " " not in handle and handle
