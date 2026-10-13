# ADR 0001: RabbitMQ quorum queues with Celery, one queue per stage

- **Status:** accepted
- **Date:** 2026-10-13
- **Code:** `services/worker/src/manifest_worker/topology.py`, `pipeline.py`, `errors.py`, `dlq.py`

## Context

A document becomes several units of work: classify it, extract each page (a model call of
5 to 15 s), reconcile once every page has answered, then classify each line's HS code and route
the shipment. The work is slow, metered by the model provider, and must survive a worker dying
mid-task. The requirements that decide the queue:

- **At-least-once with redelivery.** A task is acknowledged only after it finishes. If a worker
  crashes, the message comes back.
- **Poison containment.** A message that keeps killing workers must stop being redelivered, and
  must not be lost either.
- **Per-stage backpressure.** Slow extraction must not starve reconciliation. Each stage's
  backlog must be visible on its own.
- **Fan-out, then fan-in.** A document's pages run in parallel, and reconcile runs once after all
  of them.
- **Delayed retry without spending a worker.** Exponential backoff, and rate-limit deferral, must
  wait in the broker, not in a sleeping process.

## Decision

- **Broker and framework:** RabbitMQ 4, driven by Celery. There is one quorum queue per stage
  (`classify`, `page_extract`, `reconcile`, `hs_classify`).
- **Delivery limit and dead letters:** each queue has a delivery limit of 5. Past it, the
  message is dead-lettered to `<stage>.dlq` with at-least-once dead-lettering, so a poison
  message is parked, never dropped.
- **Acknowledgement and prefetch:** `acks_late`, `reject_on_worker_lost`, and a prefetch of 1.
  A worker holds one message at a time and acknowledges it when the task ends.
- **Fan-out and fan-in:** a Celery chord, with `page_extract` × pages as the header and
  `reconcile` as the callback. Page tasks always return, failure included, or the callback
  would never run. Results go to a Redis result backend.
- **Errors, sorted** (`manifest_worker/errors.py`):
  - transient errors autoretry with jittered exponential backoff, up to 4 times, then park in
    the DLQ;
  - content errors (invalid model output, unreadable PDF) don't retry; they go to human review;
  - configuration errors (credentials, an unwired adapter) are parked.
- **Rate limits:** our shared Redis token bucket re-queues the task with a countdown (Celery's
  native delayed delivery on quorum queues). It never spends a retry.
- **Redrive:** dead letters keep the failure in their headers. `make dlq` lists, shows, diffs and
  redrives them.

## Consequences

- **Good:**
  - A crashed worker loses nothing.
  - A poison message stops after 5 deliveries and waits in a DLQ, with its reason, for a person.
  - Queue depth per stage is the backpressure signal, measured by the monitor and alerted on
    (`DeadLetterQueueGrowing`).
  - The load test shows the backlog moving between stages as the bottleneck moves.
- **RabbitMQ 4 refuses Celery's transient control queues.** Remote control, gossip and mingle are
  off, so `celery inspect` doesn't work; the management UI and the monitor replace it.
- **Prefork needs `fork`.** On macOS, billiard spawns its children, and they can't run tasks.
  Local runs use the worker container.
- **The chord needs a result backend (Redis),** which is one more stateful dependency.
- **Celery's message protocol carries the trace context.** The API publishes `classify` through
  Celery (`send_task`), not raw AMQP, so one trace covers the request and every task it causes.

## Alternatives considered

- **Redis as the broker** (Celery or RQ):
  - visibility timeouts instead of acknowledgements, so a long model call can be redelivered
    while it's still running;
  - no native dead-lettering;
  - persistence depends on AOF settings.
- **SQS:** dead-letter queues and visibility timeouts are good, but it's managed-only (no local
  parity with compose), has no native delay past 15 minutes, and per-stage fan-in still needs
  something like the chord.
- **Kafka:** partition ordering and replay aren't needed here; per-message retry, delay and
  dead-lettering are, and Kafka makes them the application's job.
- **Postgres as the queue** (`SKIP LOCKED`): it's attractive for exactly-once with the data
  (outbox), but backoff, delivery limits, DLQs and fan-in would all be hand-built. If the
  publish-after-commit window (see `manifest_api/adapters/shipments.py`) becomes a problem, the
  first step is an outbox table drained into RabbitMQ, not replacing the broker.
