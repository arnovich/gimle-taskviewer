"""Tests for the owner-set work queue (`next:` in a task's frontmatter)."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.discovery import load_tasks
from task_viewer.queue_ops import QueueError, clear_next, enqueue, promote


def _task(root: Path, task_id: str, extra: str = "") -> Path:
    path = root / "tasks" / "open" / f"{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: Task {task_id}\nstate: open\npriority: medium\n"
        f"labels: []\n{extra}---\n\n# Task {task_id}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def tasks_dir(tmp_path: Path) -> Path:
    _task(tmp_path, "001-a")
    _task(tmp_path, "002-b")
    _task(tmp_path, "003-c")
    return tmp_path / "tasks"


def _ranks(tasks_dir: Path) -> dict[str, int | None]:
    return {task.task_id: task.next_rank for task in load_tasks(tasks_dir)}


def test_a_task_starts_unqueued(tasks_dir: Path) -> None:
    assert _ranks(tasks_dir) == {"001-a": None, "002-b": None, "003-c": None}


def test_enqueue_appends_to_the_end_of_the_queue(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    enqueue(tasks["001-a"], tasks_dir)

    assert _ranks(tasks_dir) == {"001-a": 2, "002-b": 1, "003-c": None}


def test_enqueue_is_a_no_op_for_an_already_queued_task(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    again = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]
    enqueue(again, tasks_dir)

    assert _ranks(tasks_dir)["002-b"] == 1


def test_promote_puts_a_task_at_the_front(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["001-a"], tasks_dir)
    enqueue(tasks["002-b"], tasks_dir)
    third = {t.task_id: t for t in load_tasks(tasks_dir)}["003-c"]

    promote(third, tasks_dir)
    ranks = _ranks(tasks_dir)
    assert ranks["003-c"] < ranks["001-a"]
    assert ranks["003-c"] >= 1  # the standard says positive integers


def test_promote_renumbers_only_when_it_has_to(tasks_dir: Path) -> None:
    """Gaps are cheap; rewriting every queued file is not."""
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["001-a"], tasks_dir)  # rank 1, so there is no room below
    second = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]

    promote(second, tasks_dir)
    ranks = _ranks(tasks_dir)
    assert ranks["002-b"] == 1
    assert ranks["001-a"] == 2


def test_clear_removes_the_field_entirely(tasks_dir: Path) -> None:
    tasks = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(tasks["002-b"], tasks_dir)
    queued = {t.task_id: t for t in load_tasks(tasks_dir)}["002-b"]

    clear_next(queued)
    assert _ranks(tasks_dir)["002-b"] is None
    assert "next:" not in queued.path.read_text(encoding="utf-8")


def test_clearing_an_unqueued_task_is_harmless(tasks_dir: Path) -> None:
    task = load_tasks(tasks_dir)[0]
    clear_next(task)
    assert _ranks(tasks_dir)[task.task_id] is None


def test_the_queue_ignores_closed_tasks(tmp_path: Path) -> None:
    """A finished task keeps its rank; it just stops counting."""
    _task(tmp_path, "001-a", extra="next: 1\n")
    closed = tmp_path / "tasks" / "closed"
    closed.mkdir(parents=True)
    (closed / "009-done.md").write_text(
        "---\ntitle: Done\nstate: closed\npriority: low\nlabels: []\nnext: 5\n---\n",
        encoding="utf-8",
    )
    tasks_dir = tmp_path / "tasks"
    task = _task(tmp_path, "002-b")
    enqueue(load_tasks(tasks_dir)[-1], tasks_dir)

    # 5 belongs to a closed task, so the next free rank is 2, not 6.
    assert _ranks(tasks_dir)["002-b"] == 2
    assert task.exists()


def test_a_task_without_frontmatter_cannot_be_queued(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "open" / "001-bare.md"
    path.parent.mkdir(parents=True)
    path.write_text("# No frontmatter here\n", encoding="utf-8")

    with pytest.raises(QueueError):
        enqueue(load_tasks(tmp_path / "tasks")[0], tmp_path / "tasks")


# --- the frontmatter must survive being edited ------------------------------


def _write(root: Path, task_id: str, text: str) -> Path:
    path = root / "tasks" / "open" / f"{task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_nested_next_key_is_left_alone(tmp_path: Path) -> None:
    """`  next:` two spaces in belongs to another mapping, not to the queue."""
    path = _write(
        tmp_path,
        "001-a",
        "---\ntitle: A\nstate: open\npriority: medium\nlabels: []\n"
        "claim:\n  agent: grind-7\n  next: after the refactor\n---\n\n# A\n",
    )
    tasks_dir = tmp_path / "tasks"
    enqueue(load_tasks(tasks_dir)[0], tasks_dir)

    text = path.read_text(encoding="utf-8")
    assert "  next: after the refactor" in text
    assert "\nnext: 1" in text
    assert _ranks(tasks_dir)["001-a"] == 1


def test_a_block_scalar_is_not_cut_in_half(tmp_path: Path) -> None:
    """The worst case: it still parses, so nothing errors and data is gone."""
    body = (
        "---\ntitle: A\nstate: open\npriority: medium\nlabels: []\n"
        "resolution: |\n  first line\n  next: finish the sweep\n  last line\n---\n\n# A\n"
    )
    path = _write(tmp_path, "001-a", body)
    tasks_dir = tmp_path / "tasks"
    enqueue(load_tasks(tasks_dir)[0], tasks_dir)

    text = path.read_text(encoding="utf-8")
    assert "  next: finish the sweep" in text
    assert "  last line" in text
    assert _ranks(tasks_dir)["001-a"] == 1


def test_a_differently_cased_key_is_a_different_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "001-a",
        "---\ntitle: A\nstate: open\npriority: medium\nlabels: []\n"
        "Next: some other meaning\n---\n\n# A\n",
    )
    tasks_dir = tmp_path / "tasks"
    enqueue(load_tasks(tasks_dir)[0], tasks_dir)
    assert "Next: some other meaning" in path.read_text(encoding="utf-8")


def test_crlf_line_endings_survive(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "open" / "001-a.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        b"---\r\ntitle: A\r\nstate: open\r\npriority: medium\r\nlabels: []\r\n"
        b"---\r\n\r\n# A\r\n"
    )
    tasks_dir = tmp_path / "tasks"
    enqueue(load_tasks(tasks_dir)[0], tasks_dir)

    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")  # no line was converted
    assert _ranks(tasks_dir)["001-a"] == 1


def test_a_non_utf8_file_is_refused_rather_than_mangled(tmp_path: Path) -> None:
    path = tmp_path / "tasks" / "open" / "001-a.md"
    path.parent.mkdir(parents=True)
    original = (
        b"---\ntitle: Caf\xe9 latin-1\nstate: open\npriority: medium\n"
        b"labels: []\n---\n\n# Body\n"
    )
    path.write_bytes(original)
    tasks_dir = tmp_path / "tasks"

    with pytest.raises(QueueError):
        enqueue(load_tasks(tasks_dir)[0], tasks_dir)
    assert path.read_bytes() == original  # untouched, not round-tripped to U+FFFD


def test_a_read_only_task_leaves_the_queue_untouched(tmp_path: Path) -> None:
    """A promote that cannot finish must not renumber half the queue."""
    _task(tmp_path, "010-a")
    _task(tmp_path, "020-b")
    _task(tmp_path, "099-new")
    tasks_dir = tmp_path / "tasks"
    by_id = {t.task_id: t for t in load_tasks(tasks_dir)}
    enqueue(by_id["010-a"], tasks_dir)
    enqueue(by_id["020-b"], tasks_dir)
    before = _ranks(tasks_dir)

    locked = tasks_dir / "open" / "020-b.md"
    locked.chmod(0o444)
    try:
        fresh = {t.task_id: t for t in load_tasks(tasks_dir)}["099-new"]
        with pytest.raises(QueueError):
            promote(fresh, tasks_dir)
        assert _ranks(tasks_dir) == before  # nothing moved
    finally:
        locked.chmod(0o644)


def test_a_concurrent_edit_is_refused_rather_than_clobbered(tmp_path: Path) -> None:
    """The groom pass and `grind` edit these files while tv is open."""
    from task_viewer import queue_ops

    _task(tmp_path, "001-a")
    tasks_dir = tmp_path / "tasks"
    task = load_tasks(tasks_dir)[0]

    real_read = queue_ops._read
    calls = {"n": 0}

    def racing_read(path: Path) -> str:
        calls["n"] += 1
        text = real_read(path)
        if calls["n"] == 2:  # someone else writes between our read and our write
            path.write_text(
                text.replace("state: open", "state: ongoing\nclaimed_by: grind-7"),
                encoding="utf-8",
            )
            return real_read(path)
        return text

    queue_ops._read = racing_read
    try:
        with pytest.raises(QueueError):
            enqueue(task, tasks_dir)
    finally:
        queue_ops._read = real_read
    assert "claimed_by: grind-7" in (tasks_dir / "open" / "001-a.md").read_text()


def test_a_directory_task_takes_the_rank_the_loader_reads(tmp_path: Path) -> None:
    """`analysis.md` sorts first alphabetically; the loader reads description.md."""
    entry = tmp_path / "tasks" / "open" / "070-big-task"
    entry.mkdir(parents=True)
    (entry / "description.md").write_text(
        "---\ntitle: Big\nstate: open\npriority: medium\nlabels: []\n---\n\n# Big\n",
        encoding="utf-8",
    )
    (entry / "analysis.md").write_text(
        "---\ntitle: Analysis fragment\n---\n\nNotes.\n", encoding="utf-8"
    )
    tasks_dir = tmp_path / "tasks"
    enqueue(load_tasks(tasks_dir)[0], tasks_dir)

    assert "next: 1" in (entry / "description.md").read_text(encoding="utf-8")
    assert "next:" not in (entry / "analysis.md").read_text(encoding="utf-8")
    assert _ranks(tasks_dir)["070-big-task"] == 1


def test_a_closed_task_cannot_be_queued(tmp_path: Path) -> None:
    """Closed tasks are out of the queue, so a rank there would read as nothing."""
    closed = tmp_path / "tasks" / "closed"
    closed.mkdir(parents=True)
    (closed / "009-done.md").write_text(
        "---\ntitle: Done\nstate: closed\npriority: low\nlabels: []\n---\n",
        encoding="utf-8",
    )
    tasks_dir = tmp_path / "tasks"
    task = load_tasks(tasks_dir)[0]

    with pytest.raises(QueueError):
        enqueue(task, tasks_dir)
    assert "next:" not in (closed / "009-done.md").read_text(encoding="utf-8")


def test_a_failed_write_leaves_the_original_intact(tmp_path: Path) -> None:
    """Truncating in place would shred a task file on a full disk."""
    from task_viewer import queue_ops

    _task(tmp_path, "001-a")
    tasks_dir = tmp_path / "tasks"
    path = tasks_dir / "open" / "001-a.md"
    original = path.read_text(encoding="utf-8")

    def boom(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    real_replace = queue_ops.os.replace
    queue_ops.os.replace = boom
    try:
        with pytest.raises(OSError):
            enqueue(load_tasks(tasks_dir)[0], tasks_dir)
    finally:
        queue_ops.os.replace = real_replace

    assert path.read_text(encoding="utf-8") == original
    assert not list(path.parent.glob(".*.tmp"))  # no debris left behind


def test_promote_tells_apart_two_tasks_sharing_an_id(tmp_path: Path) -> None:
    """A half-finished move leaves the same id in open/ and ongoing/.

    Identifying the queue by id would then hide the duplicate from the bump,
    leaving two tasks tied at rank 1 and the order arbitrary.
    """
    tasks_dir = tmp_path / "tasks"
    _task(tmp_path, "010-dup")  # open/, unranked — the one we promote
    ongoing = tasks_dir / "ongoing"
    ongoing.mkdir(parents=True)
    (ongoing / "010-dup.md").write_text(
        "---\ntitle: Dup\nstate: ongoing\npriority: medium\nlabels: []\nnext: 1\n---\n",
        encoding="utf-8",
    )

    fresh = {(t.state, t.task_id): t for t in load_tasks(tasks_dir)}
    promote(fresh[("open", "010-dup")], tasks_dir)

    ranks = {(t.state, t.task_id): t.next_rank for t in load_tasks(tasks_dir)}
    assert ranks[("open", "010-dup")] == 1
    assert ranks[("ongoing", "010-dup")] == 2  # bumped, not left tied at 1


def test_unparseable_frontmatter_is_reported_not_silently_accepted(
    tmp_path: Path,
) -> None:
    """Tabs make YAML invalid, so the rank is written but never read back.

    Without the check this reports success forever while nothing is queued.
    """
    path = tmp_path / "tasks" / "open" / "001-a.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n\ttitle: A\n\tstate: open\n---\n\n# A\n", encoding="utf-8"
    )
    tasks_dir = tmp_path / "tasks"

    with pytest.raises(QueueError, match="did not take effect"):
        enqueue(load_tasks(tasks_dir)[0], tasks_dir)
