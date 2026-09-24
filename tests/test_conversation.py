"""Tests for the task thread: parsing, the derived waiting state, appending."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from task_viewer.conversation import (
    ConversationError,
    Entry,
    append,
    format_entry,
    new_entry,
    open_question,
    parse,
)
from task_viewer.discovery import load_tasks
from task_viewer.textfile import TextFileError, replace_if_unchanged

THREAD = """\
# A task

## Context

Why.

## Conversation

Some intro prose nobody should parse as an entry.

### question · claude/1ff2478a · 2026-09-24T10:02:00Z

Which GPU is the reference?
Assuming the 5070 Ti.

### Answer · erikarne · 2026-09-24T12:30:00Z

The 5070 Ti.

### note · claude/1ff2478a · 2026-09-24T13:05:00Z

Done:

```
### question · not/an-entry · 2026-01-01
## Conversation
```
"""


def test_parse_returns_entries_in_order_with_kind_author_stamp_and_text() -> None:
    entries = parse(THREAD)
    assert [e.kind for e in entries] == ["question", "answer", "note"]
    assert entries[0].author == "claude/1ff2478a"
    assert entries[0].at == "2026-09-24T10:02:00Z"
    assert entries[0].text == "Which GPU is the reference?\nAssuming the 5070 Ti."
    assert entries[1].author == "erikarne"
    assert entries[2].text.startswith("Done:")
    # The fenced block stays inside the note rather than becoming entries.
    assert "not/an-entry" in entries[2].text


def test_parse_without_a_section_is_empty() -> None:
    assert parse("# Task\n\n## Context\n\nNo thread here.\n") == []
    assert parse("") == []


def test_section_ends_at_the_next_level_two_heading() -> None:
    body = THREAD + "\n## Stray section\n\n### question · x · 2026-01-01\n"
    assert len(parse(body)) == 3


def test_a_fenced_conversation_heading_is_not_a_section() -> None:
    body = "# T\n\n```\n## Conversation\n### question · a · 2026-01-01\n```\n"
    assert parse(body) == []


@pytest.mark.parametrize(
    "header",
    [
        "### question · claude/abc · 2026-09-24T10:02:00Z",
        "### question - claude/abc - 2026-09-24T10:02:00Z",
        "### question — claude/abc — 2026-09-24",
        "### question (claude/abc, 2026-09-24 10:02)",
        "### question claude/abc 2026-09-24T10:02Z",
        "### QUESTION | claude/abc | 2026-09-24T10:02:00Z",
    ],
)
def test_headers_are_read_tolerantly(header: str) -> None:
    (entry,) = parse(f"## Conversation\n\n{header}\n\nWhy?\n")
    assert entry.kind == "question"
    assert entry.author == "claude/abc"
    assert entry.at.startswith("2026-09-24")
    assert entry.when is not None
    assert entry.when.tzinfo is not None


def test_an_entry_without_author_or_stamp_still_parses() -> None:
    (entry,) = parse("## Conversation\n\n### note\n\nJust a note.\n")
    assert (entry.author, entry.at, entry.when) == ("", "", None)
    assert entry.text == "Just a note."


def _entries(*kinds: str) -> list[Entry]:
    return [Entry(kind, "who", "2026-01-01", "text") for kind in kinds]


def test_the_last_unanswered_question_is_open() -> None:
    assert open_question(_entries()) is None
    assert open_question(_entries("note")) is None
    assert open_question(_entries("question")) is not None
    assert open_question(_entries("question", "answer")) is None
    assert open_question(_entries("question", "note")) is not None
    assert open_question(_entries("question", "answer", "question")) is not None
    # One answer replies to every question above it.
    assert open_question(_entries("question", "question", "answer")) is None


def test_new_entry_is_stamped_in_utc_and_validated() -> None:
    now = datetime(2026, 9, 24, 10, 2, tzinfo=timezone.utc)
    entry = new_entry("answer", " erikarne ", "  Yes.\n", now=now)
    assert entry == Entry("answer", "erikarne", "2026-09-24T10:02:00Z", "Yes.")
    with pytest.raises(ConversationError):
        new_entry("comment", "erikarne", "text")
    with pytest.raises(ConversationError):
        new_entry("note", "erik arne", "text")
    with pytest.raises(ConversationError):
        new_entry("note", "erikarne", "   ")


def test_format_entry_round_trips_through_parse() -> None:
    entry = Entry("question", "claude/abc", "2026-09-24T10:02:00Z", "Why?\n\nReally.")
    assert parse("## Conversation\n\n" + format_entry(entry)) == [entry]


def test_append_creates_the_section_at_the_end_of_the_body(tmp_path: Path) -> None:
    md = tmp_path / "001-t.md"
    md.write_text("---\ntitle: T\n---\n\n# T\n\n## Context\n\nWhy.\n", encoding="utf-8")
    append(md, Entry("question", "claude/abc", "2026-09-24T10:02:00Z", "Which?"))
    assert md.read_text(encoding="utf-8") == (
        "---\ntitle: T\n---\n\n# T\n\n## Context\n\nWhy.\n\n"
        "## Conversation\n\n"
        "### question · claude/abc · 2026-09-24T10:02:00Z\n\nWhich?\n"
    )


def test_append_adds_to_the_end_of_an_existing_thread(tmp_path: Path) -> None:
    md = tmp_path / "001-t.md"
    md.write_text(THREAD, encoding="utf-8")
    append(md, Entry("answer", "erikarne", "2026-09-25T09:00:00Z", "Fine."))
    entries = parse(md.read_text(encoding="utf-8"))
    assert [e.kind for e in entries] == ["question", "answer", "note", "answer"]
    assert entries[-1].text == "Fine."
    assert open_question(entries) is None


def test_append_keeps_a_stray_trailing_section_after_the_thread(tmp_path: Path) -> None:
    md = tmp_path / "001-t.md"
    md.write_text(THREAD + "\n## Stray\n\nLater prose.\n", encoding="utf-8")
    append(md, Entry("note", "erikarne", "2026-09-25T09:00:00Z", "Appended."))
    text = md.read_text(encoding="utf-8")
    assert text.index("Appended.") < text.index("## Stray")
    assert text.endswith("## Stray\n\nLater prose.\n")
    assert parse(text)[-1].text == "Appended."


def test_append_preserves_crlf(tmp_path: Path) -> None:
    md = tmp_path / "001-t.md"
    md.write_bytes(b"# T\r\n\r\nBody.\r\n")
    append(md, Entry("note", "erikarne", "2026-09-25T09:00:00Z", "Hi."))
    raw = md.read_bytes()
    assert b"\r\n## Conversation\r\n\r\n### note" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_replace_refuses_when_the_file_moved_on(tmp_path: Path) -> None:
    md = tmp_path / "f.md"
    md.write_text("one\n", encoding="utf-8")
    with pytest.raises(TextFileError):
        replace_if_unchanged(md, "two\n", expected="zero\n")
    assert md.read_text(encoding="utf-8") == "one\n"
    assert list(tmp_path.iterdir()) == [md]  # no temp file left behind


def test_a_loaded_task_knows_its_open_question(tmp_path: Path) -> None:
    open_dir = tmp_path / "tasks" / "open"
    open_dir.mkdir(parents=True)
    (open_dir / "001-asked.md").write_text(
        "---\ntitle: Asked\nstate: open\n---\n\n" + THREAD.split("## Conversation")[0]
        + "## Conversation\n\n### question · claude/abc · 2026-09-24\n\nWhich?\n",
        encoding="utf-8",
    )
    (open_dir / "002-quiet.md").write_text("---\ntitle: Quiet\n---\n\nNothing.\n")
    asked, quiet = load_tasks(tmp_path / "tasks", ("open",))
    assert asked.open_question is not None
    assert asked.open_question.author == "claude/abc"
    assert quiet.open_question is None
    assert quiet.conversation == []
