"""Command-line entry point - the orchestrator.

    python -m pipelines.cli all                  # ingest -> ... -> dashboard
    python -m pipelines.cli all --offline        # rebuild from the landing zone
    python -m pipelines.cli transform            # rerun just the SQL models
    python -m pipelines.cli quality              # rerun just the assertions
    python -m pipelines.cli dashboard --focus NZL
    python -m pipelines.cli all --offline \
        --sources tests/fixtures/sources.yml --skip-volume-tests

Exit codes: 0 success, 1 a blocking data quality failure, 2 a stage failed.
A quality failure and a crash exit differently on purpose - a scheduler should
be able to tell "the data is wrong" from "the pipeline broke".
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from pipelines import dashboard, ingest, quality, stage, warehouse
from pipelines.logging_conf import configure, get_logger

log = get_logger("pipelines.cli")

EXIT_OK = 0
EXIT_QUALITY_FAILURE = 1
EXIT_STAGE_ERROR = 2


def _banner(title: str) -> None:
    log.info("─" * 66)
    log.info("▶ %s", title)


def cmd_ingest(args: argparse.Namespace) -> int:
    _banner("ingest")
    ingest.run(offline=args.offline)
    return EXIT_OK


def cmd_stage(_: argparse.Namespace) -> int:
    _banner("stage")
    stage.run()
    return EXIT_OK


def cmd_transform(_: argparse.Namespace) -> int:
    _banner("transform")
    warehouse.run()
    return EXIT_OK


def cmd_quality(args: argparse.Namespace) -> int:
    _banner("quality")
    results = quality.run(
        tests=quality.load_tests(include_volume_dependent=not args.skip_volume_tests)
    )
    summary = quality.summarise(results)
    return EXIT_QUALITY_FAILURE if summary["blocking"] else EXIT_OK


def cmd_dashboard(args: argparse.Namespace) -> int:
    _banner("dashboard")
    dashboard.run(focus=args.focus)
    return EXIT_OK


def cmd_all(args: argparse.Namespace) -> int:
    """Full pipeline. The quality gate runs before the dashboard on purpose:
    a dashboard built from data that failed its assertions is worse than no
    dashboard, so a blocking failure stops the run here."""
    started = time.monotonic()
    for step in (cmd_ingest, cmd_stage, cmd_transform):
        code = step(args)
        if code != EXIT_OK:
            return code

    code = cmd_quality(args)
    if code != EXIT_OK:
        log.error("blocking data quality failure - dashboard not refreshed")
        return code

    cmd_dashboard(args)
    log.info("─" * 66)
    log.info("pipeline completed in %.1fs", time.monotonic() - started)
    return EXIT_OK


_COMMANDS = {
    "ingest": cmd_ingest,
    "stage": cmd_stage,
    "transform": cmd_transform,
    "quality": cmd_quality,
    "dashboard": cmd_dashboard,
    "all": cmd_all,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipelines.cli",
        description="End-to-end health capacity data pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=sorted(_COMMANDS), help="pipeline stage to run")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="reuse the existing landing zone instead of fetching (ingest / all)",
    )
    parser.add_argument(
        "--focus",
        default=dashboard.DEFAULT_FOCUS,
        help="ISO alpha-3 code the dashboard leads with (default: %(default)s)",
    )
    parser.add_argument(
        "--sources",
        help="path to an alternative source registry (default: config/sources.yml)",
    )
    parser.add_argument(
        "--skip-volume-tests",
        action="store_true",
        help="skip assertions whose thresholds assume the full production feeds",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(verbose=args.verbose)
    if args.sources:
        os.environ["PIPELINE_SOURCES"] = args.sources
        log.info("using source registry %s", args.sources)
    try:
        return _COMMANDS[args.command](args)
    except Exception as error:
        log.error("%s: %s", type(error).__name__, error)
        if args.verbose:
            raise
        return EXIT_STAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
