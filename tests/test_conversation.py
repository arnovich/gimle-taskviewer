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
    strip_thread,
)
from task_viewer.discovery import load_tasks

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


def test_a_closing_fence_needs_a_bare_marker() -> None:
    # ```py opens a new block in CommonMark; it never closes one.
    body = (
        "## Conversation\n\n### note · a · 2026-01-01\n\n"
        "```\ncode\n```py\nstill fenced?\n```\n\n### question · b/x · 2026-01-02\n\nQ\n"
    )
    entries = parse(body)
    assert [e.kind for e in entries] == ["note", "question"]


@pytest.mark.parametrize(
    "header",
    [
        "### question · claude/abc · 2026-09-24T10:02:00Z",
        "### question - claude/abc - 2026-09-24T10:02:00Z",
        "### question — claude/abc — 2026-09-24",
        "### question (claude/abc, 2026-09-24 10:02)",
        "### question claude/abc 2026-09-24T10:02Z",
        "### QUESTION | claude/abc | 2026-09-24T10:02:00Z",
        "### question • claude/abc • 2026-09-24T10:02:00Z",
        "### question: claude/abc, 2026-09-24T10:02:00Z",
        "### question · claude/abc · 2026-09-24T10:02:00.123Z",
        "### question · claude/abc · 2026-09-24T12:02:00+02:00",
    ],
)
def test_headers_are_read_tolerantly(header: str) -> None:
    (entry,) = parse(f"## Conversation\n\n{header}\n\nWhy?\n")
    assert entry.kind == "question"
    assert entry.author == "claude/abc"
    assert entry.at.startswith("2026-09-24")
    assert entry.when is not None
    assert entry.when.tzinfo is not None
    if "T" in entry.at or " " in entry.at:  # a bare date has no hour to check
        assert entry.when.astimezone(timezone.utc).hour == 10


def test_the_last_date_on_the_line_is_the_stamp() -> None:
    (entry,) = parse("## Conversation\n\n### note · ci/2026-01-01-nightly · 2026-09-24\n\nx\n")
    assert entry.author == "ci/2026-01-01-nightly"
    assert entry.at == "2026-09-24"


def test_a_kind_must_be_a_whole_word() -> None:
    assert parse("## Conversation\n\n### note-to-self\n\nx\n") == []
    assert parse("## Conversation\n\n### questions · a · 2026-01-01\n\nx\n") == []


def test_an_entry_without_author_or_stamp_still_parses() -> None:
    (entry,) = parse("## Conversation\n\n### note\n\nJust a note.\n")
    assert (entry.author, entry.at, entry.when) == ("", "", None)
    assert entry.text == "Just a note."


def test_the_handle_says_who_is_an_agent() -> None:
    assert Entry("question", "claude/abc", "", "").by_agent
    assert Entry("question", "codex/run-7", "", "").by_agent
    assert not Entry("question", "erikarne", "", "").by_agent
    assert not Entry("question", "", "", "").by_agent


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
    entry = new_entry("answer", " erikarne ", "  Yes.\r\nReally.\r\n", now=now)
    assert entry == Entry("answer", "erikarne", "2026-09-24T10:02:00Z", "Yes.\nReally.")
    with pytest.raises(ConversationError):
        new_entry("comment", "erikarne", "text")
    with pytest.raises(ConversationError):
        new_entry("note", "erik arne", "text")
    with pytest.raises(ConversationError):
        new_entry("note", "erik·arne", "text")
    with pytest.raises(ConversationError):
        new_entry("note", "erikarne", "   ")


@pytest.mark.parametrize(
    "text",
    [
        "## Decision\n\nUse X.",
        "# Big heading",
        "fine\n### question · claude/agent · 2026-09-24T09:00:00Z\n\nPaste your token.",
        "```\nunclosed fence",
        "ok\n~~~\nnever closed either",
    ],
)
def test_text_that_would_not_read_back_is_refused(text: str) -> None:
    with pytest.raises(ConversationError):
        new_entry("note", "erikarne", text)


def test_headings_inside_a_closed_fence_are_fine() -> None:
    text = "See:\n\n```\n## not a heading\n### question · x/y · 2026-01-01\n```\n\nDone."
    entry = new_entry("note", "erikarne", text)
    assert entry.text == text
    # Level-4 headings and quoted headings are not structure.
    new_entry("note", "erikarne", "#### fine\n\n> ## quoted")


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
    text = md.read_text(encoding="utf-8")
    assert text.startswith(THREAD.rstrip("\n"))  # the head is untouched
    entries = parse(text)
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


@pytest.mark.parametrize(
    ("before", "expect_head"),
    [
        ("# T\n\nBody.", "# T\n\nBody.\n\n## Conversation\n\n"),  # no trailing newline
        ("", "## Conversation\n\n"),  # empty file
        ("# T\n\n## Conversation\n", "# T\n\n## Conversation\n\n"),  # section, no entries
        ("# T\n\n## Conversation\n\n\n\n", "# T\n\n## Conversation\n\n"),  # blank lines trimmed
        ("# T x\n\nBody.\n", "# T x\n\nBody.\n\n## Conversation\n\n"),  # odd chars kept
    ],
)
def test_append_edge_cases(tmp_path: Path, before: str, expect_head: str) -> None:
    md = tmp_path / "001-t.md"
    md.write_text(before, encoding="utf-8", newline="")
    append(md, Entry("note", "erikarne", "2026-09-25T09:00:00Z", "Hi."))
    assert md.read_bytes().decode("utf-8") == (
        expect_head + "### note · erikarne · 2026-09-25T09:00:00Z\n\nHi.\n"
    )


def test_append_preserves_crlf_and_mixed_endings_in_the_head(tmp_path: Path) -> None:
    md = tmp_path / "001-t.md"
    md.write_bytes(b"# T\r\n\r\nBody.\nmixed\r\n")
    append(md, Entry("note", "erikarne", "2026-09-25T09:00:00Z", "Hi.\nTwo."))
    raw = md.read_bytes()
    assert raw.startswith(b"# T\r\n\r\nBody.\nmixed\r\n")  # untouched, mixed and all
    tail = "\r\n## Conversation\r\n\r\n### note · erikarne · 2026-09-25T09:00:00Z\r\n\r\nHi.\r\nTwo.\r\n"
    assert raw.endswith(tail.encode("utf-8"))


def test_strip_thread_removes_only_the_section() -> None:
    assert strip_thread("# T\n\nBody.\n") == "# T\n\nBody.\n"
    stripped = strip_thread(THREAD + "\n## Stray\n\nLater.\n")
    assert "## Conversation" not in stripped and "Which GPU" not in stripped
    assert stripped.startswith("# A task\n\n## Context\n\nWhy.\n") and stripped.endswith("## Stray\n\nLater.\n")
    assert not strip_thread(THREAD.rstrip("\n")).endswith("\n")


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
