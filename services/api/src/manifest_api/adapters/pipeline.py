"""Hand a finalized document to the pipeline: ``manifest.classify`` on the ``classify`` queue.

The API and the worker share no code; they meet at the broker. These names are the worker's
(``manifest_worker.app`` task routes, ``manifest_worker.topology``), and a worker test checks
they still match. The message is published through Celery's own protocol, so OpenTelemetry's
Celery instrumentation writes the request's trace context into its headers and the worker's
spans continue the finalize request's trace. Nothing is declared from here: the worker owns
the topology (quorum queues, dead-lettering) and declares it on start.
"""

import asyncio
from uuid import UUID

from celery import Celery

CLASSIFY_TASK = "manifest.classify"
EXCHANGE = "manifest"
CLASSIFY_ROUTING_KEY = "classify"


class CeleryPipeline:
    def __init__(self, broker_url: str) -> None:
        self._celery = Celery("manifest-api", broker=broker_url, set_as_current=False)
        self._celery.conf.update(
            broker_transport_options={"confirm_publish": True},
            task_serializer="json",
            accept_content=["json"],
        )

    async def enqueue(self, document_id: UUID, job_id: UUID) -> None:
        """Queue classification; ``job_id`` becomes the task id (``FinalizeAccepted.job_id``)."""
        await asyncio.to_thread(self._send, document_id, job_id)

    def _send(self, document_id: UUID, job_id: UUID) -> None:
        self._celery.send_task(
            CLASSIFY_TASK,
            args=[str(document_id)],
            task_id=str(job_id),
            exchange=EXCHANGE,
            routing_key=CLASSIFY_ROUTING_KEY,
            declare=[],
        )

    def close(self) -> None:
        self._celery.close()
