"""Tests for the tv-web entry point: argument handling and startup failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from task_viewer.web import cli

from helpers import git, init_repo


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    """Capture what would have been served instead of starting uvicorn."""
    calls: list[dict] = []

    def fake_run(app, host, port, log_level):
        calls.append({"app": app, "host": host, "port": port})

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "alpha.git"
    bare.mkdir()
    git(bare, "init", "--bare", "-b", "main")
    seed = init_repo(tmp_path / "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return bare


def test_no_repositories_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, served) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cli.main([]) == 2
    assert "no repositories" in capsys.readouterr().err
    assert served == []


def test_a_broken_config_is_a_usage_error(tmp_path: Path, capsys, served) -> None:
    config = tmp_path / "web.toml"
    config.write_text("port = 'eighty'\n")
    assert cli.main(["--config", str(config), "--repo", "https://x/y.git"]) == 2
    assert "must be numbers" in capsys.readouterr().err


def test_two_repositories_with_one_name_are_refused(tmp_path: Path, capsys, served) -> None:
    code = cli.main(["--repo", "https://a/x.git", "--repo", "https://b/x.git", "--data-dir", str(tmp_path / "d")])
    assert code == 2
    assert "share a name" in capsys.readouterr().err


def test_a_repository_that_cannot_be_cloned_stops_startup(tmp_path: Path, capsys, served) -> None:
    code = cli.main(["--repo", str(tmp_path / "missing.git"), "--data-dir", str(tmp_path / "d")])
    assert code == 1
    assert "clone failed" in capsys.readouterr().err
    assert served == []


def test_startup_clones_dedupes_and_passes_host_and_port(tmp_path: Path, origin: Path, served, capsys) -> None:
    data = tmp_path / "d"
    code = cli.main([
        "--repo", str(origin), "--repo", str(origin), "--data-dir", str(data),
        "--owner", "me", "--host", "0.0.0.0", "--port", "0",
    ])
    assert code == 0
    assert (data / "alpha" / ".git").is_dir()
    assert [p.name for p in data.iterdir()] == ["alpha"]
    assert served and served[0]["host"] == "0.0.0.0" and served[0]["port"] == 0
    assert "answering as me" in capsys.readouterr().out
