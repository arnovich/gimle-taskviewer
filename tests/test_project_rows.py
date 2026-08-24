"""Tests for how a project's git state is worded in the list and the pane.

The row and the summary section are what the user actually reads, so these
assert on rendered text. The row markers are driven off constructed
:class:`GitInfo` values — no repository needed to check the wording matrix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from task_viewer.app import (
    _ROW_BRANCH_WIDTH,
    _WORKTREE_BRANCH_WIDTH,
    _format_project_row,
    _format_worktree_row,
    _dirty_note,
    _drift_note,
    _format_git_line,
    _git_section,
    _plural,
)
from task_viewer.git_info import GitInfo
from task_viewer.workspace import Project

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def _project(name: str) -> Project:
    """A project whose tasks folder does not exist — counts come back zero."""
    return Project(name, Path("/nowhere") / name, Path("/nowhere") / name / "tasks")


def _info(**overrides: object) -> GitInfo:
    """A plain unmerged worktree, one commit ahead, with fields to override."""
    defaults = dict(
        branch="feat/thing",
        is_worktree=True,
        created=_NOW - timedelta(days=3),
        updated=_NOW - timedelta(hours=2),
        base="main",
        repo="gimle-example",
        ahead=1,
        behind=0,
        subjects=["Add the feature"],
        dirty=0,
    )
    defaults.update(overrides)
    return GitInfo(**defaults)  # type: ignore[arg-type]


# --- the compact row line ---------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"ahead": 3}, "↑3"),
        ({"ahead": 1, "behind": 7}, "↓7"),
        ({"dirty": 4}, "✎4"),
        ({"ahead": 0, "behind": 7, "subjects": []}, "✔merged"),
        # A plain clone in sync with its remote still shows branch and age.
        ({"ahead": 0, "is_worktree": False, "subjects": []}, "feat/thing"),
    ],
)
def test_row_markers(overrides: dict, expected: str) -> None:
    assert expected in _format_git_line(_info(**overrides))


def test_a_synced_clone_claims_no_marker() -> None:
    line = _format_git_line(_info(ahead=0, is_worktree=False, subjects=[]))
    assert "merged" not in line
    assert "↑" not in line and "↓" not in line


def test_row_always_carries_the_age() -> None:
    """Even the longest shape — merged and dirty — must still show it."""
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    line = _format_git_line(_info(ahead=0, subjects=[], dirty=10, updated=recent))
    assert "✔merged" in line and "✎10" in line
    assert "2h" in line


def test_row_shows_the_age_of_an_untouched_checkout() -> None:
    assert "—" in _format_git_line(_info(updated=None))


def test_no_row_line_without_git() -> None:
    assert _format_git_line(None) == ""


def test_long_branch_names_are_ellipsised() -> None:
    line = _format_git_line(_info(branch="feat/" + "x" * 40))
    shown = line.split("⎇ ")[1].split("[/]")[0]
    assert len(shown) == _ROW_BRANCH_WIDTH
    assert shown.endswith("…")


def test_a_short_branch_name_is_left_alone() -> None:
    assert "⎇ feat/thing[/]" in _format_git_line(_info())


def test_branch_names_cannot_inject_markup() -> None:
    """A branch is user data; Rich would otherwise read `[bold]` as a tag."""
    line = _format_git_line(_info(branch="fix/[bold]parse"))
    assert r"\[bold]" in line


# --- the summary section ----------------------------------------------------


def test_section_answers_the_three_questions() -> None:
    section = "\n".join(_git_section(_info()))
    assert "## Worktree" in section
    assert "worktree of `gimle-example`" in section
    assert "**Created**" in section
    assert "**Updated**" in section
    assert "**Base** `main`" in section
    assert "- Add the feature" in section


def test_a_clone_reports_branch_since_not_created() -> None:
    section = "\n".join(_git_section(_info(is_worktree=False, repo=None)))
    assert "**Branch since**" in section
    assert "**Created**" not in section


def test_no_section_without_git() -> None:
    assert _git_section(None) == []


def test_merged_worktree_says_so_instead_of_counting_drift() -> None:
    note = _drift_note(_info(ahead=0, behind=9, subjects=[]))
    assert "**Merged** fully into `main`" in note
    assert "9 behind" not in note


def test_synced_clone_says_nothing_unpushed() -> None:
    note = _drift_note(_info(ahead=0, is_worktree=False, subjects=[]))
    assert "**In sync** with `main`" in note


def test_drift_note_reports_behind() -> None:
    assert "221 behind" in _drift_note(_info(ahead=2, behind=221))


def test_drift_note_without_a_base() -> None:
    note = _drift_note(_info(base=None, ahead=0, subjects=[]))
    assert "no `main`/`master` branch" in note


def test_merged_worktree_with_uncommitted_files_warns() -> None:
    """The one state where the obvious action — remove it — destroys work."""
    section = "\n".join(_git_section(_info(ahead=0, subjects=[], dirty=10)))
    assert "⚠" in section
    assert "10 files here are uncommitted" in section


def test_an_unmerged_worktree_is_not_warned_about() -> None:
    assert "⚠" not in "\n".join(_git_section(_info(dirty=10)))


def test_a_clean_merged_worktree_is_not_warned_about() -> None:
    assert "⚠" not in "\n".join(_git_section(_info(ahead=0, subjects=[])))


def test_summary_says_how_many_commits_were_not_listed() -> None:
    section = "\n".join(_git_section(_info(ahead=7, subjects=["a", "b", "c"])))
    assert "### 7 commits not in `main`" in section
    assert "- …and 4 more" in section


def test_no_leftover_line_when_everything_is_listed() -> None:
    section = "\n".join(_git_section(_info(ahead=2, subjects=["a", "b"])))
    assert "…and" not in section


@pytest.mark.parametrize(
    ("dirty", "expected"),
    [(0, ""), (1, " · 1 uncommitted file"), (2, " · 2 uncommitted files")],
)
def test_dirty_note_pluralisation(dirty: int, expected: str) -> None:
    assert _dirty_note(_info(dirty=dirty)) == expected


@pytest.mark.parametrize(
    ("count", "expected"), [(1, "1 commit"), (2, "2 commits"), (0, "0 commits")]
)
def test_plural(count: int, expected: str) -> None:
    assert _plural(count, "commit") == expected


# --- worktree rows nested under a repo --------------------------------------


def test_a_worktree_row_leads_with_its_branch() -> None:
    row = _format_worktree_row(_project("gimle-example-feature"), _info())
    assert "⎇ feat/thing" in row
    assert "↑1" in row


def test_a_worktree_row_gets_a_narrower_branch_budget() -> None:
    """It is indented under its repo, so it has less room than a repo row."""
    long_branch = _info(branch="feat/" + "x" * 40)
    nested = _format_worktree_row(_project("wt"), long_branch)
    shown = nested.split("⎇ ")[1].split("[/]")[0]
    assert len(shown) == _WORKTREE_BRANCH_WIDTH
    assert _WORKTREE_BRANCH_WIDTH < _ROW_BRANCH_WIDTH


def test_a_worktree_row_falls_back_to_its_folder_name() -> None:
    row = _format_worktree_row(_project("gimle-example-feature"), None)
    assert "gimle-example-feature" in row


def test_a_collapsed_repo_reports_what_is_folded_away() -> None:
    row = _format_project_row(
        _project("gimle-mimir"),
        _info(branch="main", is_worktree=False),
        worktrees=[_info(ahead=0, subjects=[]), _info(ahead=0, subjects=[]), _info()],
        expanded=False,
    )
    assert "▸" in row
    assert "3 wt" in row
    assert "2 merged" in row


def test_an_expanded_repo_drops_the_folded_note() -> None:
    row = _format_project_row(
        _project("gimle-mimir"),
        _info(branch="main", is_worktree=False),
        worktrees=[_info(ahead=0, subjects=[])],
        expanded=True,
    )
    assert "▾" in row
    assert "wt" not in row


def test_a_repo_without_worktrees_gets_no_marker() -> None:
    row = _format_project_row(_project("gimle-hugin"), _info(), worktrees=[])
    assert "▸" not in row and "▾" not in row


def test_a_collapsed_repo_with_nothing_merged_says_only_the_count() -> None:
    row = _format_project_row(
        _project("gimle-mimir"), _info(), worktrees=[_info(), _info()], expanded=False
    )
    assert "2 wt" in row
    assert "merged" not in row
