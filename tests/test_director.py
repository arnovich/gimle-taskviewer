"""Tests for what the director derives: whose move, who is running, what is next."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_viewer.discovery import Task
from task_viewer.history import Commit
from task_viewer.web.director import (
    STALE_AFTER,
    RepoFacts,
    agents,
    attention,
    feed,
    timeline,
    up_next,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _task(task_id: str, state: str = "open", rank: int | None = None, thread: str = "", **meta) -> Task:
    body = f"# {task_id}\n" + thread
    return Task(
        task_id, task_id.split("-", 1)[1].replace("-", " ").title(), state,
        Path(f"/r/tasks/{state}/{task_id}.md"), body, next_rank=rank, meta=dict(meta),
        description=body,
    )


def _q(author: str, at: str = "2026-09-24T10:00:00Z") -> str:
    return f"\n## Conversation\n\n### question · {author} · {at}\n\nWhy?\n"


def test_attention_sorts_the_owners_moves_into_buckets() -> None:
    repo = RepoFacts("r", [
        _task("001-asked", thread=_q("claude/a")),
        _task("002-mine", thread=_q("erikarne")),
        _task("003-done", "closed"),
        _task("004-stuck", thread="\n## Attempts\n\n- one\n- two\n"),
        _task("005-once", thread="\n## Attempts\n\n- one\n"),
        _task("006-twin-a"), _task("006-twin-b"),
    ], branch_tips={"task/003_done": NOW})
    found = attention([repo])
    assert [w.task.task_id for w in found.questions] == ["001-asked"]
    assert [w.task.task_id for w in found.asked] == ["002-mine"]
    assert [(r.task.task_id, r.branch) for r in found.ready] == [("003-done", "task/003_done")]
    assert [(g.task.task_id, g.attempts) for g in found.gave_up] == [("004-stuck", 2)]
    assert [(a.number, len(a.tasks)) for a in found.ambiguous] == [("006", 2)]
    assert found.count == 4
    assert found.summary == "1 question, 1 branch ready to merge, 1 task given up on, 1 ambiguous number"


def test_agents_are_grouped_by_handle_with_the_last_sign_of_life() -> None:
    fresh = NOW - timedelta(minutes=10)
    old = NOW - STALE_AFTER - timedelta(hours=1)
    repo = RepoFacts("r", [
        _task("001-a", "ongoing", claimed_by="claude/x", claimed_at=old.isoformat(), branch="task/001_a",
              thread="\n## Conversation\n\n### note · claude/x · " + (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n\nHalfway.\n"),
        _task("002-b", "ongoing", claimed_by="claude/x", claimed_at=old.isoformat()),
        _task("003-c", "ongoing", claimed_by="codex/y", claimed_at=old.isoformat()),
        _task("004-d", "open"),
    ], branch_tips={"task/001_a": fresh})
    x, y = agents([repo])
    assert x.handle == "claude/x" and [t.task_id for _, t in x.holds] == ["001-a", "002-b"]
    assert x.since == old and x.last_commit == fresh and x.last_branch == "task/001_a"
    assert x.last_entry is not None and x.last_entry.text == "Halfway."
    assert x.last_seen == fresh and not x.stale
    assert y.handle == "codex/y" and y.last_seen == old and y.stale


def test_up_next_gives_the_reason_a_ranked_task_is_held() -> None:
    repo = RepoFacts("r", [
        _task("001-asked", rank=1, thread=_q("claude/a")),
        _task("002-blocked", rank=2, depends_on=["001-asked"]),
        _task("003-free", rank=3, depends_on=["009-gone"]),  # an unknown dependency does not block
        _task("004-busy", "ongoing", rank=4, claimed_by="claude/z"),
        _task("005-stuck", rank=5, thread="\n## Attempts\n\n- a\n- b\n"),
        _task("006-twin", rank=6), _task("006-twin-again"),
        _task("007-closed", "closed", rank=7),
        _task("008-owner-asked", rank=8, thread=_q("erikarne")),
    ])
    picks = up_next(repo)
    assert [(p.task.task_id, p.reason) for p in picks] == [
        ("001-asked", "waiting on your answer"),
        ("002-blocked", "blocked by 001-asked"),
        ("003-free", ""),
        ("004-busy", "claimed by claude/z"),
        ("005-stuck", "given up after 2 attempts"),
        ("006-twin", "number 006 is ambiguous"),
        ("008-owner-asked", ""),
    ]


def test_the_feed_merges_commits_and_entries_and_drops_the_duplicate() -> None:
    task = _task("001-a", thread="\n## Conversation\n\n### answer · erikarne · 2026-09-24T11:00:00Z\n\nYes.\n")
    commits = [
        Commit("1", "erikarne", NOW - timedelta(hours=1), "task 001: answer from erikarne"),
        Commit("2", "claude", NOW - timedelta(hours=2), "task 001: claim"),
        Commit("3", "old", NOW - timedelta(days=30), "task 001: filed"),
        Commit("4", "gh", NOW - timedelta(hours=3), "Merge pull request #9 from x/task/001_a"),
    ]
    events = feed([RepoFacts("r", [task], commits)], now=NOW)
    assert [(e.kind, e.who) for e in events] == [("answer", "erikarne"), ("claimed", "claude"), ("merged", "gh")]
    assert events[0].text == "Yes." and events[2].pull_request == 9
    assert all(e.task_id == "001-a" for e in events)


def test_the_timeline_is_one_tasks_history_oldest_first() -> None:
    task = _task("001-a", thread="\n## Conversation\n\n### note · claude/x · 2026-09-24T10:30:00Z\n\nHalf.\n")
    other = _task("002-b")
    commits = [
        Commit("1", "claude", NOW - timedelta(hours=1), "task 001: plan"),
        Commit("2", "claude", NOW - timedelta(hours=3), "task 001: claim"),
        Commit("3", "claude", NOW - timedelta(hours=2), "task 002: claim"),
    ]
    events = timeline(RepoFacts("r", [task, other], commits), task)
    assert [e.kind for e in events] == ["claimed", "note", "planned"]
