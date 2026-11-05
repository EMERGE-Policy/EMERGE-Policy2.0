"""The `emerge` command: the TUI on a terminal; anywhere else (a pipe, a script) emerge's headless mode."""

from __future__ import annotations

import asyncio
import sys

from emerge.application import StartupError
from emerge.cli import arguments, headless, version_text
from emerge.exceptions import ConfigError
from tui.app import run_tui


def main(argv: list[str] | None = None) -> int:
    args = arguments("emerge").parse_args(argv)
    if args.version:
        print(version_text())
        return 0
    try:
        if sys.stdin.isatty():
            return asyncio.run(run_tui(args.workspace, args.session))
        return asyncio.run(headless(args.workspace, args.session))
    except (StartupError, ConfigError) as e:
        print(f"emerge: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
