"""Where the control plane finds its repositories, and who is answering."""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..mirror import DEFAULT_MAX_AGE

DEFAULT_PORT = 8765


class ConfigError(Exception):
    """Raised when the configuration cannot be used."""


@dataclass
class Config:
    """Everything ``tv-web`` needs to start."""

    repos: list[str] = field(default_factory=list)
    owner: str = ""
    data_dir: Path = field(default_factory=lambda: default_data_dir())
    max_age: float = DEFAULT_MAX_AGE
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tv" / "web.toml"


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tv" / "mirrors"


def load_config(path: Path | None) -> Config:
    """Read the TOML file at ``path``; a missing default file is just empty."""
    config = Config()
    if path is None:
        path = default_config_path()
        if not path.is_file():
            return config
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error

    repos = data.get("repos", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) for r in repos):
        raise ConfigError(f"{path}: `repos` must be a list of URLs")
    config.repos = repos
    config.owner = str(data.get("owner", "") or "")
    if "data_dir" in data:
        config.data_dir = Path(str(data["data_dir"])).expanduser()
    if "max_age" in data:
        config.max_age = float(data["max_age"])
    config.host = str(data.get("host", config.host))
    config.port = int(data.get("port", config.port))
    return config


def resolve_owner(configured: str) -> str:
    """The handle written on the owner's entries.

    Configured wins; otherwise git's ``user.name`` with the spaces squeezed
    out, since an author is one token; otherwise ``owner``.
    """
    if configured.strip():
        return configured.strip()
    try:
        proc = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return "owner"
    name = "-".join(proc.stdout.split()) if proc.returncode == 0 else ""
    return name or "owner"
