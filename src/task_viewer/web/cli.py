"""Command-line entry point for ``tv-web``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..mirror import Mirror, MirrorError, name_from_url
from .app import create_app
from .config import ConfigError, default_config_path, load_config, resolve_owner
from .control import ControlPlane


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tv-web",
        description="Serve the control plane: the task files of many repositories, "
        "read from the app's own clones and written back by commit and push.",
    )
    parser.add_argument(
        "--config", type=Path, default=None, metavar="PATH",
        help=f"TOML config (default: {default_config_path()})",
    )
    parser.add_argument(
        "--repo", action="append", default=[], metavar="URL",
        help="A repository to watch; repeatable. Added to the config's list.",
    )
    parser.add_argument("--data-dir", type=Path, default=None, metavar="DIR",
                        help="Where the clones live (default: from config, or ~/.local/share/tv/mirrors).")
    parser.add_argument("--owner", default=None, help="Handle written on your entries.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"tv-web: {error}", file=sys.stderr)
        return 2
    repos = list(dict.fromkeys(config.repos + args.repo))
    if not repos:
        print(
            "tv-web: no repositories. Pass --repo URL or list them under `repos` in "
            f"{args.config or default_config_path()}.",
            file=sys.stderr,
        )
        return 2
    data_dir = (args.data_dir or config.data_dir).expanduser()
    owner = resolve_owner(args.owner or config.owner)

    mirrors = []
    for url in repos:
        try:
            mirrors.append(Mirror(url, data_dir / name_from_url(url)))
        except MirrorError as error:
            print(f"tv-web: {error}", file=sys.stderr)
            return 2
    names = [m.name for m in mirrors]
    if len(set(names)) != len(names):
        print("tv-web: two repositories share a name; they would share a clone", file=sys.stderr)
        return 2

    for mirror in mirrors:
        try:
            print(f"tv-web: {mirror.name}: {'ready' if (mirror.root / '.git').exists() else 'cloning…'}", flush=True)
            mirror.ensure()
        except MirrorError as error:
            print(f"tv-web: {error}", file=sys.stderr)
            return 1

    control = ControlPlane(mirrors, owner, config.max_age)
    host, port = args.host or config.host, args.port or config.port
    print(f"tv-web: answering as {owner} · http://{host}:{port}/", flush=True)

    import uvicorn

    uvicorn.run(create_app(control), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
