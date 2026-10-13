# ADR 0003: Degradation policy: shed early, retry what's transient, route the rest to people

- **Status:** accepted
- **Date:** 2026-10-13
- **Code:** `services/api/src/manifest_api/adapters/shipments.py` (admission),
  `services/worker/src/manifest_worker/errors.py`, `pipeline.py`, `shipments.py` (routing),
  `libs/domain/src/manifest_domain/routing.py`, `libs/llm/src/manifest_llm/gateway.py`

## Context

Manifest's output is a decision: auto-approve a shipment, or send it to a person. A degraded
system can fail in two directions:

- **Loudly:** errors and lost work.
- **Quietly:** auto-approving something it shouldn't have.

The second is worse. A customs declaration filed on a wrong HS code or quantity costs more than a
delayed one. So when parts of the system are slow, missing or wrong, it has to be clear which way
each failure resolves.

## Decision

**Fail closed on decisions, fail soft on work.** Every failure mode either retries invisibly,
waits in a place someone can see, or lands in the human review queue. None of them can produce
an auto-approval.

| When | The system | Observable as |
|---|---|---|
| More pages pending than the high-water mark | The API refuses new finalizes: 429 with Retry-After, checked atomically when the document is queued (under an advisory lock, so a simultaneous burst can't slip past a stale count). The client retries with the same Idempotency-Key. | 429 responses; queue depth |
| Our shared model budget is spent (Redis token bucket) | Short waits sleep in-process. Longer ones re-queue the task with a countdown, which doesn't count as a retry. | `rate_limiter_wait_seconds{outcome}` |
| A transient failure (provider 429/5xx or timeout, storage or database unreachable) | Autoretry with jittered exponential backoff, up to 4 times, then park in `<stage>.dlq` with the reason. A page's failure never blocks its document's chord. | Celery retries; `dlq_depth`; alert `DeadLetterQueueGrowing` |
| The model answers outside the schema, refuses, or is truncated | No retry: the page fails as content, and the shipment goes to review (PARTIAL_EXTRACTION). | `pages_processed_total{status="failed"}`; review reasons |
| A value isn't on its page verbatim (evidence check fails) | Its confidence becomes 0, so it can never clear an auto-approve threshold. | Field confidence; evals |
| Documents disagree, or a required field is missing | Discrepancies are recorded and the shipment goes to review. Disagreement never quietly lowers a confidence. | `discrepancies`; review reasons |
| No embedding key or index for vector search | HS retrieval runs on Postgres full-text search alone: a measured, lower-recall configuration. (A transient embedding error is retried, not degraded.) | `retriever` on each suggestion's walk |
| The HS walk answers "none of these", cites something it wasn't shown, or HS is off | The line gets no suggestion: HS_UNCERTAIN goes to review. | Review reasons |
| No calibrator pinned, or one fitted on stand-in answers or to another scoring recipe | Confidences stay raw and nothing auto-approves. | `calibrator` on each row |
| An adapter isn't wired (no model key; the review API) | Pages are parked in the DLQ (rejected, not retried). Unwired endpoints answer 501. | DLQ; 501s |
| The monitor that samples queues stops | Alert `MonitorDown`, because without it the queue and review alerts can't fire. | Prometheus `absent()` |

Auto-approval happens only when the following all hold:
- every required field and every HS suggestion clears a trusted calibrator's threshold;
- no discrepancy is open;
- no review task is open.

Routing never overrides a reviewer's approval.

## Consequences

- **Correctness costs volume:** in the load test with HS on, 9% of shipments auto-approved and
  the rest went to people. Raising that number is a model and calibration question, measured by
  the evals, never a matter of relaxing the policy.
- **Overload shows up at the edge,** as 429s the client handles, rather than as a growing backlog
  that pushes every request past what clients will wait. Admitted work keeps its latency.
- **Every degraded path is observable.** Each row has a metric, an alert or a review reason. The
  demo GIF exercises the first and third rows on the real system.
- **No second model to degrade to.** A failing HS walk sends lines to review rather than to a
  cheaper model. A fallback model would need its own calibrator before its answers could be
  trusted.

## Alternatives considered

- **Best-effort answers** (auto-approve on the extraction alone when HS or calibration is
  unavailable): it keeps volume up and is quietly wrong. Rejected, because confidence is the
  product.
- **Unbounded intake** (no admission control): every request is accepted, then the backlog
  makes every request late. Shedding at a known depth keeps admitted work inside the targets.
- **Infinite retries:** a poison message would loop forever. The delivery limit and DLQ make
  failure visible and redrivable instead.
