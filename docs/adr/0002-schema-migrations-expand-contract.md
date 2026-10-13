# ADR 0002: Versioned extraction schemas, expand/contract migrations, out-of-band backfills

- **Status:** accepted
- **Date:** 2026-10-13
- **Code:** `libs/domain/src/manifest_domain/schemas/`, `libs/db/src/manifest_db/migrations/versions/`,
  `services/worker/src/manifest_worker/backfills/`

## Context

What we extract changes. v2 added each line's country of origin. v3 added provenance: the page,
a verbatim evidence span and the confidence signals. Each change raises three problems:

- **Old rows:** existing extractions were read under the old schema, and their confidences were
  scored under the old recipe.
- **Rolling deploys:** a running deploy reads tables a migration is changing, and API and workers
  roll at different times.
- **Backfill cost:** re-extracting means calling the model again. That costs money, takes hours,
  and must be resumable and rate-limited like any other work.

Calibration makes this sharper. A calibrator is fitted to one scoring recipe and one schema, so
mixing versions silently would corrupt the confidence numbers the product is about.

## Decision

- **Every extraction row carries `schema_version`.** A released `schemas/vN` is frozen;
  extracting something new means a new version.
- **Re-extraction never edits a row.** It inserts a new `extractions` row and sets
  `superseded_at` on the old one. A partial unique index allows one current extraction per
  document. History stays, and evals can compare versions.
- **Schema changes deploy in three steps, expand/contract:**
  1. **Expand:** a migration adds nullable columns, defaults and NOT VALID constraints, and the
     code that writes the new version ships with it. Old and new code both work against it.
  2. **Backfill, out of band:** `manifest_worker.backfills` re-extracts only the affected
     documents. Each backfill is registered in `backfill_runs`, single-runner (an advisory
     lock), resumable, and goes through the same queues, rate limits and DLQs as live traffic.
     It never runs inside a migration.
  3. **Contract:** a migration validates constraints and flips defaults. It refuses to apply
     while the backfill has work left (0005 checks origins on v1 documents; 0008 checks for
     pre-v3 extractions).
- **Migrations are frozen snapshots.** They never import the ORM models or domain enums, spell
  out every type and label, and name constraints through `NAMING_CONVENTION`.
- **History stays linear and reversible:**
  - one Alembic head;
  - a stairway test downgrades and re-applies every step;
  - `alembic check` fails on drift between models and migrations;
  - CI runs up → check → down → up.
- **Prompt changes aren't schema versions.** How the model is asked is versioned as
  `PROMPT_VERSION`. Rewording a prompt changes the eval cassettes' keys and needs a
  recalibration, not a migration.

## Consequences

- **Good:**
  - Any deploy order is safe.
  - A failed backfill resumes where it stopped.
  - A contract migration can't strand data.
  - Evals and calibrators always know which schema and recipe produced a number.
- **More steps per change:** three deploys and a backfill instead of one migration. The
  contract's guard turns "forgot the backfill" into a refused deploy instead of an outage.
- **Storage grows with history** (superseded extractions are kept). That's acceptable at this
  volume; pruning would be a backfill of its own.
- **Reference data ships vendored and checksummed** (HS 2022 nomenclature), never fetched at
  migrate time, so a migration's result doesn't depend on a third-party server.

## Alternatives considered

- **Migrate in place** (one migration that rewrites old rows): it would need the model inside a
  migration, which is slow, paid and non-transactional, and it would lose the old answers.
- **Store a version only in JSON payloads:** there would be nothing for constraints or partial
  indexes to enforce, and old and new rows would mix in queries.
- **Big-bang deploys with downtime:** simpler, but API and workers are separate deployables and
  in-flight messages survive a deploy, so some code always reads a schema it didn't write.
