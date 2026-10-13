"""Queue ``hs_classify``: an HS 2022 suggestion per goods line, then route the shipment.

Runs after ``reconcile_shipment``, for the fingerprint it reconciled: if a newer extraction has
been reconciled since, this run is stale and stops (the newer one has its own). Lines that
already have a suggestion are skipped, so a re-run after a rate-limit deferral doesn't pay
again; each suggestion is upserted. The raw confidence is calibrated with the pinned HS
calibrator. A line the classifier can't answer for is left without a suggestion (it counts as
confidence 0). Then the shipment is routed: review, or auto-approve (``shipments.route``).

Skipped classification (no classifier wired in) still routes, and fails closed: HS_UNCERTAIN.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from manifest_db.models import Document, HsSuggestion, Shipment
from manifest_domain.routing import decide
from manifest_llm.errors import RateLimitedError
from manifest_telemetry import metrics
from manifest_worker import shipments
from manifest_worker.app import celery_app
from manifest_worker.errors import CONTENT_ERRORS
from manifest_worker.pipeline import PipelineTask, defer
from manifest_worker.runtime import runtime

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, base=PipelineTask, name="manifest.hs_classify", stage="hs_classify")
def hs_classify(self: PipelineTask, shipment_id: str, fingerprint: str) -> dict[str, Any]:
    rt = runtime()
    target = UUID(shipment_id)
    with rt.sessions() as session:
        shipment = session.get(Shipment, target)
        if shipment is None:
            # A message from before classification was per shipment: (document_id, version).
            document = session.get(Document, target)
            if document is not None:
                celery_app.send_task(
                    "manifest.reconcile_shipment", args=[str(document.shipment_id)]
                )
            return {"shipment_id": shipment_id, "skipped": "not a shipment"}
        if shipment.reconciled_fingerprint != fingerprint:
            return {"shipment_id": shipment_id, "skipped": "stale: reconciled again since"}
        lines = shipments.lines_to_classify(session, target) if rt.hs_classifier else []

    calibrator = rt.calibrators.hs
    classified = 0
    for line in lines:
        assert rt.hs_classifier is not None  # noqa: S101 - lines are only listed with one
        try:
            result = rt.hs_classifier.classify(line)
        except RateLimitedError as exc:
            defer(self, exc.retry_after)  # lines done so far are skipped on the re-run
        except CONTENT_ERRORS as exc:
            logger.warning("no HS suggestion for line %s: %s", line.line_item_id, exc)
            continue
        values = {
            "code": result.code,
            "confidence": calibrator.apply(result.confidence) if calibrator else result.confidence,
            "raw_confidence": result.confidence,
            "calibrator": calibrator.label if calibrator else None,
            "rationale": result.rationale,
            "retrieved_candidates": result.candidates,
            "walk": result.walk,
            "model": result.model,
        }
        with rt.sessions.begin() as session:
            statement = insert(HsSuggestion).values(line_item_id=line.line_item_id, **values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["line_item_id"], set_={**values, "created_at": func.now()}
                )
            )
        classified += 1

    with rt.sessions.begin() as session:
        shipment = session.get(Shipment, target)
        if shipment is None or shipment.reconciled_fingerprint != fingerprint:
            return {"shipment_id": shipment_id, "classified": classified, "skipped": "stale"}
        calibrators = (rt.calibrators.extraction, rt.calibrators.hs)
        found = shipments.results(
            session, target, calibrators, classified=rt.hs_classifier is not None
        )
        decision = decide(found)
        status = shipments.route(session, target, decision)
    if status is not None:
        metrics.shipments_routed.add(1, {"decision": str(status)})
    return {
        "shipment_id": shipment_id,
        "classified": classified,
        "status": str(status),
        "reasons": sorted(decision.reasons),
    }
