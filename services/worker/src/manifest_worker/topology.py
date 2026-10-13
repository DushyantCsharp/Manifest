"""RabbitMQ topology: one quorum queue per pipeline stage, each dead-lettering to its own DLQ.

    exchange "manifest" (topic) --<stage>--> <stage>         quorum, x-delivery-limit=5
    exchange "manifest.dlx" (direct) --<stage>.dlq--> <stage>.dlq

A message lands in ``<stage>.dlq`` when the broker gives up on it (returned more than the
delivery limit, e.g. it keeps killing workers) or when a task parks it after exhausting its
retries. ``python -m manifest_worker.dlq`` inspects, diffs and redrives them.
"""

from kombu import Connection, Exchange, Queue

STAGES = ("classify", "page_extract", "reconcile", "hs_classify")
DELIVERY_LIMIT = 5

# Topic, not direct: Celery's native delayed delivery (countdowns and retry backoff on quorum
# queues) only works through topic exchanges.
EXCHANGE = Exchange("manifest", type="topic", durable=True)
DEAD_LETTER_EXCHANGE = Exchange("manifest.dlx", type="direct", durable=True)


def dlq_name(stage: str) -> str:
    return f"{stage}.dlq"


def work_queue(stage: str) -> Queue:
    return Queue(
        stage,
        EXCHANGE,
        routing_key=stage,
        durable=True,
        queue_arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": DELIVERY_LIMIT,
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE.name,
            "x-dead-letter-routing-key": dlq_name(stage),
            # Quorum-only: dead-lettering is confirmed, so a poison message is never lost.
            "x-dead-letter-strategy": "at-least-once",
            "x-overflow": "reject-publish",
        },
    )


def dead_letter_queue(stage: str) -> Queue:
    # Classic, not quorum: inspecting a DLQ requeues its messages, and that must not count
    # towards a delivery limit (RabbitMQ 4 quorum queues default to 20 and then drop).
    return Queue(
        dlq_name(stage),
        DEAD_LETTER_EXCHANGE,
        routing_key=dlq_name(stage),
        durable=True,
        queue_arguments={"x-queue-type": "classic"},
    )


WORK_QUEUES = tuple(work_queue(stage) for stage in STAGES)
DEAD_LETTER_QUEUES = tuple(dead_letter_queue(stage) for stage in STAGES)


def declare(connection: Connection) -> None:
    """Declare every queue and exchange (idempotent). Workers do this on start."""
    channel = connection.default_channel
    for queue in (*WORK_QUEUES, *DEAD_LETTER_QUEUES):
        queue(channel).declare()
