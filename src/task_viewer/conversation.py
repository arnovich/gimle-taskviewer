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
did not ask. The two parties are told apart by the author alone — an agent's
handle follows the ``claimed_by`` scheme, ``<family>/<id>``, so a ``/`` means
agent and anything else means the owner.

The format is fixed by ``~/.agents/references/task-format.md``; this module
reads it tolerantly (any of the usual separators, a bare date, headings inside
fenced code ignored, everything column-0 only) and writes it canonically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .textfile import read_exact, replace_if_unchanged

KINDS = ("question", "answer", "note")
SECTION_HEADING = "## Conversation"
SEPARATOR = " · "  # U+00B7 MIDDLE DOT, with a space each side

_SECTION_RE = re.compile(r"^##[ \t]+Conversation[ \t]*$", re.IGNORECASE)
_LEVEL2_RE = re.compile(r"^##[ \t]+\S")
_ENTRY_RE = re.compile(
    r"^###[ \t]+(question|answer|note)(?![\w-])(.*)$", re.IGNORECASE
)
# A stamp is the LAST thing on the line, so a date inside an author is safe.
_STAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
# Anything an author might reasonably have typed between the three fields.
_SEPARATOR_RE = re.compile(r"[·•—–|,:]|\s-\s|\(|\)")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# Headings of the levels the format itself uses. An entry is a reply; it
# cannot contain one without being misread as structure.
_HEADING_RE = re.compile(r"^#{1,3}(?:[ \t]|$)")
_AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@-]*$")


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
    def by_agent(self) -> bool:
        """True for an agent's handle (``claude/<session>``), false for a person's."""
        return "/" in self.author

    @property
    def when(self) -> datetime | None:
        """The timestamp as an aware datetime, or ``None`` when it does not parse."""
        stamp = self.at.strip().replace(" ", "T")
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
    """An entry stamped now, validated the way the standard requires.

    The text is checked to survive a round trip through :func:`parse`: no
    headings of the levels the format uses, no unclosed code fence, nothing
    that would read back as a second entry under someone else's name.
    """
    if kind not in KINDS:
        raise ConversationError(f"unknown entry kind: {kind!r}")
    author = author.strip()
    if not _AUTHOR_RE.match(author):
        raise ConversationError(
            "author must be one handle of letters, digits and ._/@- only"
        )
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ConversationError("an entry needs some text")
    fence: str | None = None
    for line, fenced in _scan(text.splitlines()):
        if not fenced and _HEADING_RE.match(line):
            raise ConversationError(
                "an entry cannot contain a heading (`#`, `##`, `###` at the start "
                "of a line) — quote it with `>` or put it in a code block"
            )
        fence = _FENCE_RE.match(line).group(1) if fenced and fence is None else fence
    if _unclosed_fence(text):
        raise ConversationError("an entry cannot leave a code fence unclosed")
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entry = Entry(kind, author, stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), text)
    if parse(f"{SECTION_HEADING}\n\n{format_entry(entry)}") != [entry]:
        raise ConversationError("the entry would not read back as written")
    return entry


def format_entry(entry: Entry) -> str:
    """The canonical markdown for one entry."""
    header = SEPARATOR.join((entry.kind, entry.author, entry.at))
    return f"### {header}\n\n{entry.text}\n"


def append(md_file: Path, entry: Entry) -> None:
    """Add ``entry`` at the end of the file's thread, creating it if needed.

    Everything before the insertion point is kept byte for byte. The section
    stays where it is if something was written after it — the entry still
    lands at the end of the *thread*, never after a stray section.
    """
    original = read_exact(md_file)
    newline = "\r\n" if "\r\n" in original else "\n"
    cr = "\r" if newline == "\r\n" else ""
    raw = original.split("\n")
    if raw and raw[-1] == "":
        raw.pop()  # the final newline, put back below
    bounds = _section_bounds([line.rstrip("\r") for line in raw])
    if bounds is None:
        head, tail = raw, []
        block = ["", SECTION_HEADING]
    else:
        head, tail = raw[: bounds[1]], raw[bounds[1] :]
        block = []
    while head and not head[-1].strip():
        head.pop()
    block += ["", *format_entry(entry).rstrip("\n").split("\n")]
    if not head:
        block = block[1:]
    block = [line + cr for line in block]
    updated = head + block + ([cr, *tail] if tail else [])
    # Every line already carries its own "\r" when the file uses CRLF.
    replace_if_unchanged(md_file, "\n".join(updated) + "\n", original)


def strip_thread(body: str) -> str:
    """The body with its ``## Conversation`` section removed."""
    lines = body.splitlines()
    bounds = _section_bounds(lines)
    if bounds is None:
        return body
    start, end = bounds
    kept = "\n".join(lines[:start] + lines[end:]).rstrip()
    return kept + ("\n" if body.endswith("\n") else "")


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
            closes = (
                marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not line[match.end():].strip()
            )
            if closes:
                fence = None
                yield line, True
                continue
        yield line, fence is not None


def _unclosed_fence(text: str) -> bool:
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if not match:
            continue
        marker = match.group(1)
        if fence is None:
            fence = marker
        elif marker[0] == fence[0] and len(marker) >= len(fence) and not line[match.end():].strip():
            fence = None
    return fence is not None


def _entry(header: re.Match[str], text: list[str]) -> Entry:
    rest = header.group(2)
    stamps = list(_STAMP_RE.finditer(rest))
    at = stamps[-1].group(0) if stamps else ""
    if stamps:
        rest = rest[: stamps[-1].start()] + rest[stamps[-1].end() :]
    tokens = [token.strip() for token in _SEPARATOR_RE.split(rest)]
    author = next((token for token in tokens if token), "")
    return Entry(header.group(1).lower(), author, at, "\n".join(text).strip())
