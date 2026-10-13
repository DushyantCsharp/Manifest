"""Run a data backfill: ``python -m manifest_worker.backfills <name> [options]``.

country-of-origin   v1 documents lacking line-item origins (unblocks migration 0005)
schema-v3           documents not yet on extraction schema v3 (unblocks migration 0008)
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from manifest_db.settings import DatabaseSettings
from manifest_worker.backfills import country_of_origin, reextract, schema_v3
from manifest_worker.backfills.ratelimit import TokenBucket
from manifest_worker.ports import UnwiredExtractor
from manifest_worker.runtime import runtime

SPECS = {"country-of-origin": country_of_origin, "schema-v3": schema_v3}


async def _run(args: argparse.Namespace) -> reextract.Outcome:
    module = SPECS[args.backfill]
    engine = create_async_engine(DatabaseSettings().url)
    try:
        return await reextract.run(
            module.SPEC,
            engine,
            module.celery_enqueue,
            limiter=TokenBucket(args.rate, burst=max(1, int(args.rate))),
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            restart=args.restart,
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m manifest_worker.backfills")
    backfills = parser.add_subparsers(dest="backfill", required=True)
    helps = {
        "country-of-origin": "re-extract v1 documents lacking country_of_origin (unblocks 0005)",
        "schema-v3": "re-extract documents not yet on extraction schema v3 (unblocks 0008)",
    }
    for name, text in helps.items():
        sub = backfills.add_parser(name, help=text)
        sub.add_argument("--batch-size", type=int, default=100)
        sub.add_argument("--rate", type=float, default=2.0, help="documents enqueued per second")
        sub.add_argument("--max-batches", type=int, help="stop after N batches (resume later)")
        sub.add_argument("--restart", action="store_true", help="discard the saved cursor")
    args = parser.parse_args(argv)

    if isinstance(runtime().extractor, UnwiredExtractor):
        print(
            "warning: the extraction stage isn't implemented yet; enqueued re-extractions will "
            "fail until it is",
            file=sys.stderr,
        )
    outcome = asyncio.run(_run(args))
    state = "pass complete" if outcome.completed else "stopped early; rerun to resume"
    summary = f"enqueued {outcome.enqueued} in {outcome.batches} batches, {state}"
    print(f"{SPECS[args.backfill].NAME}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
