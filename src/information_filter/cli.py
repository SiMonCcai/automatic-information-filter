"""Command-line interface."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from .config import BUILTINS, load_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aif", description="Collect, filter, and publish information from a TOML pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run a configured pipeline once")
    run.add_argument("-c", "--config", default="pipeline.toml")
    validate = subparsers.add_parser("validate", help="Load and validate a configuration")
    validate.add_argument("-c", "--config", default="pipeline.toml")
    subparsers.add_parser("plugins", help="List built-in plugin types")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plugins":
        print(json.dumps({key: sorted(value) for key, value in BUILTINS.items()}, indent=2))
        return 0
    pipeline = load_pipeline(args.config)
    if args.command == "validate":
        print(f"Configuration is valid: {args.config}")
        return 0
    stats = pipeline.run()
    print(json.dumps(stats.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
