"""Tests for what the director derives: whose move, who is running, what is next."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from task_viewer.discovery import Task
from task_viewer.github import Pull, Run, Snapshot
from task_viewer.history import Commit
from task_viewer.web.director import (
    STALE_AFTER,
    RepoFacts,
    agents,
    attention,
    ci,
    feed,
    main_health,
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


def _run(id, workflow, branch, status, conclusion="", when=NOW) -> Run:
    return Run(id, workflow, f"run {id}", status, conclusion, branch, "push", when, when, f"https://x/{id}")


def _pull(number, branch, checks="passing", updated=NOW) -> Pull:
    return Pull(number, f"PR {number}", branch, f"https://x/pull/{number}", False, "claude", "", "MERGEABLE", checks, 1, 1, updated, updated)


def test_attention_sorts_the_owners_moves_into_buckets() -> None:
    snapshot = Snapshot(pulls=[_pull(7, "task/003_done"), _pull(8, "feat/other")], checked=NOW)
    repo = RepoFacts("r", [
        _task("001-asked", thread=_q("claude/a")),
        _task("002-mine", thread=_q("erikarne")),
        _task("003-done", "closed"),
        _task("004-stuck", thread="\n## Attempts\n\n- one\n- two\n"),
        _task("005-once", thread="\n## Attempts\n\n- one\n"),
        _task("006-twin-a"), _task("006-twin-b"),
        _task("007-merged", "closed"),
    ], branch_tips={"task/003_done": NOW, "task/007_merged": NOW - timedelta(days=3)}, github=snapshot)
    found = attention([repo])
    assert [w.task.task_id for w in found.questions] == ["001-asked"]
    assert [w.task.task_id for w in found.asked] == ["002-mine"]
    assert [(p.pull.number, p.task.task_id if p.task else None) for p in found.pulls] == [(7, "003-done"), (8, None)]
    # A closed task's branch with an open PR is a PR; one without is a stray.
    assert [(s.task.task_id, s.branch) for s in found.strays] == [("007-merged", "task/007_merged")]
    assert [(g.task.task_id, g.attempts) for g in found.gave_up] == [("004-stuck", 2)]
    assert [(a.number, len(a.tasks)) for a in found.ambiguous] == [("006", 2)]
    assert found.count == 5 and found.unknown == []
    assert found.summary == "1 question, 2 pull requests to review, 1 task given up on, 1 ambiguous number"


def test_without_github_no_branch_is_called_anything() -> None:
    repo = RepoFacts("r", [_task("003-done", "closed")], branch_tips={"task/003_done": NOW})
    found = attention([repo])
    assert found.pulls == [] and found.strays == [] and found.unknown == []
    failed = RepoFacts("r", [_task("003-done", "closed")], branch_tips={"task/003_done": NOW}, github=Snapshot(pulls_error="gh: not logged in"))
    found = attention([failed])
    assert found.pulls == [] and found.strays == [] and found.unknown == ["r"]


def test_ci_reports_live_runs_and_the_latest_real_failure_per_workflow_and_branch() -> None:
    old = NOW - timedelta(days=2)
    snapshot = Snapshot(runs=[
        _run(1, "tests", "task/001_a", "in_progress"),
        _run(2, "tests", "main", "queued"),
        _run(3, "lint", "main", "completed", "failure", NOW - timedelta(hours=1)),
        _run(4, "lint", "main", "completed", "success", NOW - timedelta(hours=2)),   # older than the failure
        _run(5, "tests", "main", "completed", "failure", NOW - timedelta(hours=3)),
        _run(6, "tests", "main", "completed", "success", NOW - timedelta(hours=1)),  # newer: the failure is fixed
        _run(7, "tests", "task/009_z", "completed", "failure", old),                 # too old to act on
        _run(8, "smoke", "task/001_a", "completed", "failure", NOW - timedelta(hours=2)),
        _run(9, "smoke", "task/001_a", "completed", "cancelled", NOW - timedelta(hours=1)),  # decides nothing
        _run(10, "docs", "main", "completed", "failure", old),                        # old, but main stays failing
    ], checked=NOW)
    facts = RepoFacts("r", [_task("001-a", "ongoing")], github=snapshot)
    found = ci([facts], {"r": "main"}, now=NOW)
    assert [(r.run.id, r.task.task_id if r.task else None) for r in found.running] == [(1, "001-a")]
    assert [r.run.id for r in found.queued] == [2]
    assert [r.run.id for r in found.failed] == [3, 8, 10]
    assert found.summary == "1 run running, 1 run queued, 3 failures"
    assert ci([RepoFacts("r", [], github=Snapshot(runs_error="down"))], now=NOW).unknown == ["r"]
    assert ci([RepoFacts("r", [])], now=NOW).quiet


def test_main_health_is_the_latest_completed_run_per_workflow() -> None:
    snapshot = Snapshot(runs=[
        _run(1, "tests", "main", "completed", "success", NOW),
        _run(2, "lint", "main", "completed", "failure", NOW - timedelta(hours=1)),
        _run(3, "lint", "main", "completed", "success", NOW),  # newer than the failure
        _run(4, "tests", "task/001_a", "completed", "failure", NOW),  # not main
    ], checked=NOW)
    assert main_health(RepoFacts("r", [], github=snapshot), "main") == "passing"
    snapshot.runs.append(_run(5, "lint", "main", "completed", "failure", NOW + timedelta(minutes=1)))
    assert main_health(RepoFacts("r", [], github=snapshot), "main") == "failing"
    # A cancelled or skipped run after the failure decides nothing; a run in progress does not clear it either.
    snapshot.runs.append(_run(6, "lint", "main", "completed", "cancelled", NOW + timedelta(minutes=2)))
    snapshot.runs.append(_run(7, "lint", "main", "completed", "skipped", NOW + timedelta(minutes=3)))
    snapshot.runs.append(_run(8, "lint", "main", "in_progress", "", NOW + timedelta(minutes=4)))
    assert main_health(RepoFacts("r", [], github=snapshot), "main") == "failing"
    live = Snapshot(runs=[_run(9, "tests", "main", "in_progress")], checked=NOW)
    assert main_health(RepoFacts("r", [], github=live), "main") == "running"
    only_skipped = Snapshot(runs=[_run(10, "tests", "main", "completed", "skipped")], checked=NOW)
    assert main_health(RepoFacts("r", [], github=only_skipped), "main") == ""  # nothing ever passed
    assert main_health(RepoFacts("r", []), "main") == ""
    assert main_health(RepoFacts("r", [], github=Snapshot(runs_error="x")), "main") == ""


def test_drafts_and_changes_requested_are_the_agents_move() -> None:
    ready = _pull(7, "task/003_done")
    draft = Pull(8, "PR 8", "task/004_x", "https://x/pull/8", True, "claude", "", "MERGEABLE", "pending", 0, 1, NOW, NOW)
    sent_back = Pull(9, "PR 9", "feat/y", "https://x/pull/9", False, "claude", "CHANGES_REQUESTED", "MERGEABLE", "passing", 1, 1, NOW, NOW)
    repo = RepoFacts("r", [_task("003-done", "closed"), _task("004-x", "ongoing")], github=Snapshot(pulls=[ready, draft, sent_back], checked=NOW))
    found = attention([repo])
    assert [p.pull.number for p in found.pulls] == [7]
    assert [p.pull.number for p in found.theirs] == [8, 9]
    assert found.count == 1 and found.summary == "1 pull request to review"
    assert found.pull_for("r", repo.tasks[1]).pull.number == 8  # the draft still belongs to its task
    assert found.pull_for("r", repo.tasks[0]).pull.number == 7


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
        Commit("4", "gh", NOW - timedelta(hours=3), "Merge pull request #9 from x/task/001_a", body="Do the thing"),
        Commit("5", "gh", NOW - timedelta(hours=4), "Merge pull request #10 from x/feat/other", body="Something else"),
    ]
    events = feed([RepoFacts("r", [task], commits)], now=NOW)
    assert [(e.kind, e.who) for e in events] == [
        ("answer", "erikarne"), ("claimed", "claude"), ("merged", "gh"), ("merged", "gh"),
    ]
    assert events[0].text == "Yes."
    assert events[1].text == ""  # "claim" says nothing beyond the verb
    assert events[2].pull_request == 9 and events[2].text == "Do the thing" and events[2].task_id == "001-a"
    # A merge that is not a task's is named by its branch, and still says what landed.
    assert events[3].task_id is None and events[3].task_title == "feat/other" and events[3].text == "Something else"


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
