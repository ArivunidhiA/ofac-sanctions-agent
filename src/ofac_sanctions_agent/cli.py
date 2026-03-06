from __future__ import annotations

import argparse
import asyncio
from typing import List, Optional

from .agent import run_agent, run_dry_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OFAC SDN sanctions screening agent (async Playwright)"
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        default=False,
        help="Run browser in visible (non-headless) mode",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=250,
        metavar="MS",
        help=(
            "Milliseconds of artificial delay between Playwright actions "
            "(default: 250)"
        ),
    )
    parser.add_argument(
        "--no-remarks",
        action="store_true",
        default=False,
        help="Skip fetching per-row remarks (faster, less detail)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Validate config and test OFAC portal connectivity, then exit "
            "without running any searches. Useful for pre-deployment checks."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.dry_run:
        return asyncio.run(run_dry_run())

    asyncio.run(
        run_agent(
            headless=not args.visible,
            slow_mo=args.slow_mo,
            fetch_remarks=not args.no_remarks,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

