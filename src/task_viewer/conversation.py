"""The thread at the bottom of a task: questions, answers and notes.

A task's ``## Conversation`` section is how the owner and the agents talk. An
agent that needs a decision asks there, the owner answers there, and the
exchange lives in the repo next to the task. The section is append-only and
always last in the body; each entry is a level-3 heading naming its kind, its
author and when it was written, then the text::

    ### question · claude/1ff2478a · 2026-09-24T10:02:00Z

    Which GPU is the reference?

Whether a task is *waiting* on someone is derived, never stored: the last
``question`` with no ``answer`` after it is open, and the task waits on whoever
did not ask. The format is fixed by ``gimle-skills/references/task-format.md``;
this module reads it tolerantly and writes it canonically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .textfile import read_exact, replace_if_unchanged

KINDS = ("question", "answer", "note")
SECTION_HEADING = "## Conversation"

_SECTION_RE = re.compile(r"^##[ \t]+Conversation[ \t]*$", re.IGNORECASE)
_LEVEL2_RE = re.compile(r"^##[ \t]+\S")
_ENTRY_RE = re.compile(r"^###[ \t]+(question|answer|note)\b(.*)$", re.IGNORECASE)
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?")
# Anything an author might reasonably have typed between the three fields.
_SEPARATOR_RE = re.compile(r"[·—–|,]|\s-\s|\(|\)")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


class ConversationError(Exception):
    """Raised when an entry cannot be written."""


@dataclass(frozen=True)
class Entry:
    """One entry of the thread, as written."""

    kind: str
    author: str
    at: str
    text: str

    @property
    def when(self) -> datetime | None:
        """The timestamp as a datetime, or ``None`` when it does not parse."""
        stamp = self.at.replace(" ", "T")
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def parse(body: str) -> list[Entry]:
    """Every entry of the body's ``## Conversation`` section, in order.

    Prose before the first entry is ignored, and headings inside fenced code
    are never mistaken for entries.
    """
    lines = body.splitlines()
    bounds = _section_bounds(lines)
    if bounds is None:
        return []
    start, end = bounds
    entries: list[Entry] = []
    header: re.Match[str] | None = None
    text: list[str] = []
    for line, fenced in _scan(lines[start + 1 : end]):
        match = None if fenced else _ENTRY_RE.match(line)
        if match is None:
            text.append(line)
            continue
        if header is not None:
            entries.append(_entry(header, text))
        header, text = match, []
    if header is not None:
        entries.append(_entry(header, text))
    return entries


def open_question(entries: list[Entry]) -> Entry | None:
    """The last question nothing has answered, or ``None``.

    An ``answer`` replies to every question above it, so the scan runs from
    the end: the first answer met closes the thread, the first question met
    before one is open. Notes say nothing either way.
    """
    for entry in reversed(entries):
        if entry.kind == "answer":
            return None
        if entry.kind == "question":
            return entry
    return None


def new_entry(
    kind: str, author: str, text: str, now: datetime | None = None
) -> Entry:
    """An entry stamped now, validated the way the standard requires."""
    if kind not in KINDS:
        raise ConversationError(f"unknown entry kind: {kind!r}")
    author = author.strip()
    if not author or any(char.isspace() for char in author):
        raise ConversationError("author must be one handle with no spaces")
    text = text.strip()
    if not text:
        raise ConversationError("an entry needs some text")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return Entry(kind, author, stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), text)


def format_entry(entry: Entry) -> str:
    """The canonical markdown for one entry."""
    return f"### {entry.kind} · {entry.author} · {entry.at}\n\n{entry.text}\n"


def append(md_file: Path, entry: Entry) -> None:
    """Add ``entry`` at the end of the file's thread, creating it if needed.

    The section stays where it is if something was written after it — the
    entry still lands at the end of the *thread*, never after a stray section.
    """
    original = read_exact(md_file)
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines()
    bounds = _section_bounds(lines)
    if bounds is None:
        head, tail = lines, []
        block = ["", SECTION_HEADING]
    else:
        head, tail = lines[: bounds[1]], lines[bounds[1] :]
        block = []
    while head and not head[-1].strip():
        head.pop()
    block += ["", *format_entry(entry).rstrip("\n").splitlines()]
    updated = head + block + ([""] + tail if tail else [])
    replace_if_unchanged(md_file, newline.join(updated) + newline, original)


def _section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """``(heading index, end index)`` of the section, or ``None`` if absent."""
    start: int | None = None
    for index, (line, fenced) in enumerate(_scan(lines)):
        if fenced:
            continue
        if start is None:
            if _SECTION_RE.match(line):
                start = index
        elif _LEVEL2_RE.match(line):
            return start, index
    return None if start is None else (start, len(lines))


def _scan(lines: list[str]):
    """Yield ``(line, inside_fence)`` so headings in code blocks are skipped."""
    fence: str | None = None
    for line in lines:
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
                yield line, True
                continue
            if marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
                yield line, True
                continue
        yield line, fence is not None


def _entry(header: re.Match[str], text: list[str]) -> Entry:
    rest = header.group(2)
    stamp = _STAMP_RE.search(rest)
    at = stamp.group(0) if stamp else ""
    if stamp:
        rest = rest[: stamp.start()] + rest[stamp.end() :]
    tokens = [token.strip() for token in _SEPARATOR_RE.split(rest)]
    author = next((token for token in tokens if token), "")
    return Entry(header.group(1).lower(), author, at, "\n".join(text).strip())
