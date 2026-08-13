"""Worker service loop: poll -> execute -> sleep.

This is the thing that makes OpFlow actually automate: every round runs the
M3 poller (enqueue date-scoped low-stock jobs, idempotent) followed by the
M4/M6 worker round (recover stale, claim, run the capability-gated agent,
complete). No external scheduler needed - the loop IS the scheduler.

CLI:
    python -m app.runner                 # run forever, 60s between rounds
    python -m app.runner --once          # single poll + worker round, then exit
    python -m app.runner --interval 30   # override the sleep between rounds
"""
from __future__ import annotations

import argparse
import logging
import time
from typing import Any

from app.db import SessionLocal
from app.poller import poll_low_stock
from app.worker import run_worker_round

logger = logging.getLogger("opflow.runner")

DEFAULT_INTERVAL_SECONDS = 60


def run_worker_once() -> dict[str, Any]:
    """One poll + one worker round against the live DB.

    Returns a summary: the job ids the poller touched (new or pre-existing,
    so a date already polled yields ids without rework) and the ids the
    worker actually processed this round.
    """
    with SessionLocal() as session:
        touched = poll_low_stock(session)
        processed = run_worker_round(session)

    return {
        "jobs_polled": [job.id for job in touched],
        "jobs_processed": processed,
    }


def run_worker_forever(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    """Run rounds on a timer until interrupted (Ctrl+C / SIGINT)."""
    logger.info("worker started (interval=%ss)", interval_seconds)
    while True:
        summary = run_worker_once()
        logger.info(
            "round complete: %d job(s) touched by poll, %d processed",
            len(summary["jobs_polled"]),
            len(summary["jobs_processed"]),
        )
        time.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OpFlow worker service loop (poll + execute + sleep)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single poll + worker round, then exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="seconds between rounds (default: %(default)s)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if args.once:
            print(run_worker_once())
        else:
            run_worker_forever(args.interval)
    except KeyboardInterrupt:
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
