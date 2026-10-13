"""Out-of-band data backfills that expand/contract migrations wait on.

Each backfill is resumable (checkpoint in ``backfill_runs``), single-runner (Postgres advisory
lock) and rate-limited, so it can run alongside live traffic. Run with
``python -m manifest_worker.backfills <name>``.
"""
