"""The whole pipeline on golden packs, with the model's answers replayed from cassettes.

RabbitMQ, Redis, Postgres and MinIO are real (testcontainers). The only stand-ins are the model,
whose answers come from ``evals/cassettes`` keyed by request hash (so replayed calls still pass
the shared token bucket and output validation), and OCR, which returns the text the corpus
snapshot recorded for a scanned page (tesseract builds differ). Each pack's invoice, packing
list and bill of lading are uploaded, then classified, split, read, extracted, normalised,
evidence-checked and merged, and every stored value and discrepancy must equal what the offline
eval harness reads and reconciles from the same cassettes. (How close those are to the ground
truth is the harness's to measure: the cassettes hold a real model's answers.)
"""

import hashlib
import json
import re
import secrets
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from celery.contrib.testing.worker import start_worker
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import (
    Discrepancy,
    Document,
    Extraction,
    LineItem,
    Page,
    ReviewTask,
    Shipment,
)
from manifest_db.session import sync_session_factory
from manifest_db.testing import alembic_config, scratch_database
from manifest_domain.alignment import align, line_distance
from manifest_domain.extraction import CURRENT_SCHEMA_VERSION
from manifest_domain.lifecycle import DocumentKind, DocumentStatus, ShipmentStatus
from manifest_domain.review import ReviewReason
from manifest_evals import golden
from manifest_evals.run import evaluate_pack, expected_findings
from manifest_llm.cassette import CassetteProvider
from manifest_llm.gateway import LlmGateway
from manifest_llm.ratelimit import Limits, TokenBucket
from manifest_worker import dlq
from manifest_worker.app import celery_app
from manifest_worker.extraction import MODEL, LlmPageExtractor
from manifest_worker.runtime import Runtime, use_runtime
from manifest_worker.storage import S3Storage
from manifest_worker.tasks.classify import classify
from manifest_worker.text import split_pages

pytestmark = pytest.mark.integration

# One of each layout family worth a full pipeline run: German numbers and dates, pounds and long
# dates, a scanned (OCR) French compact invoice, and a pack with deliberate mismatches.
PACKS = ["clean-0002", "clean-0004", "scan-0005", "mism-0001"]
DONE = {DocumentStatus.EXTRACTED, DocumentStatus.PARTIALLY_EXTRACTED, DocumentStatus.FAILED}


def test_the_chosen_packs_have_cassettes() -> None:
    assert set(PACKS) <= set(golden.golden_packs())
    for pack in PACKS:
        assert len(list((golden.CASSETTES / pack).glob("*.json"))) == 3


# What PDFium stamps on every save: a random trailer ID and the time of the save.
_VOLATILE = re.compile(rb"/ID\[<[0-9A-F]+><[0-9A-F]+>\]|/(?:Creation|Mod)Date\([^)]*\)")


def page_key(page_pdf: bytes) -> str:
    """A page's identity: its bytes, minus what PDFium changes on every save."""
    return hashlib.sha256(_VOLATILE.sub(b"", page_pdf)).hexdigest()


def snapshot_ocr() -> Any:
    """OCR that answers each page with the text the corpus snapshot recorded for it."""
    recorded: dict[str, str] = {}
    for pack in PACKS:
        snapshot = json.loads((golden.GOLDEN / pack / "pages.json").read_text())
        for document, pages in snapshot.items():
            pdf = (golden.GOLDEN / pack / f"{document}.pdf").read_bytes()
            for page, read in zip(split_pages(pdf), pages, strict=True):
                recorded[page_key(page)] = read["text"]

    def ocr(page_pdf: bytes) -> str:
        return recorded[page_key(page_pdf)]

    return ocr


@pytest.fixture
def pipeline(
    celery_broker: str, postgres_url: str, redis_url: str, object_store: Any, tmp_path: Path
) -> Iterator[dict[str, Any]]:
    # One replaying provider for every pack: keys are hashes, so recordings can share a folder.
    for pack in PACKS:
        shutil.copytree(golden.CASSETTES / pack, tmp_path / "cassettes", dirs_exist_ok=True)
    provider = CassetteProvider(tmp_path / "cassettes", name=golden.PROVIDER)
    with scratch_database(postgres_url) as database:
        command.upgrade(alembic_config(database), "head")
        redis = Redis.from_url(redis_url)
        namespace = f"manifest-test-{secrets.token_hex(4)}"
        gateway = LlmGateway(
            {provider.name: provider},
            TokenBucket(redis, namespace=namespace),
            default_limits=Limits(requests_per_minute=600, tokens_per_minute=10_000_000),
        )
        storage = S3Storage(
            object_store.endpoint_url,
            object_store.bucket,
            object_store.access_key,
            object_store.secret_key,
            "us-east-1",
        )
        sessions = sync_session_factory(database)
        extractor = LlmPageExtractor(gateway, provider.name, MODEL)
        use_runtime(Runtime(sessions, storage, extractor, hs_classifier=None, ocr=snapshot_ocr()))
        with start_worker(
            celery_app, pool="threads", concurrency=4, perform_ping_check=False, shutdown_timeout=30
        ):
            yield {
                "sessions": sessions,
                "storage": storage,
                "provider": provider,
                "vhost": celery_broker,
            }
        use_runtime(None)
        for key in redis.scan_iter(f"{namespace}:*"):
            redis.delete(key)


def upload(pipeline: dict[str, Any], pack: str) -> dict[str, UUID]:
    """The pack's three PDFs as one shipment's documents, keyed by file stem."""
    documents = {}
    with pipeline["sessions"].begin() as session:
        shipment = Shipment(reference=pack)
        session.add(shipment)
        session.flush()
        for pdf in sorted((golden.GOLDEN / pack).glob("*.pdf")):
            key = f"golden/{pack}/{secrets.token_hex(4)}/{pdf.name}"
            pipeline["storage"].put(key, pdf.read_bytes(), "application/pdf")
            document = Document(
                shipment_id=shipment.id,
                filename=pdf.name,
                content_type="application/pdf",
                size_bytes=pdf.stat().st_size,
                storage_key=key,
            )
            session.add(document)
            session.flush()
            documents[pdf.stem] = document.id
    return documents


ROUTED = {ShipmentStatus.NEEDS_REVIEW, ShipmentStatus.AUTO_APPROVED}


def settled(sessions: sessionmaker[Session], ids: list[UUID]) -> bool:
    """Every document done, and every shipment reconciled and routed."""
    with sessions() as session:
        documents = session.scalars(select(Document).where(Document.id.in_(ids))).all()
        shipment_ids = {d.shipment_id for d in documents}
        routed = session.scalars(select(Shipment.status).where(Shipment.id.in_(shipment_ids)))
        return all(d.status in DONE for d in documents) and all(s in ROUTED for s in routed)


def stuck(sessions: sessionmaker[Session], ids: list[UUID]) -> str:
    """Why the pipeline didn't settle: each unfinished document and its pages' errors."""
    report = []
    with sessions() as session:
        for document in session.scalars(select(Document).where(Document.id.in_(ids))):
            if document.status in DONE:
                continue
            pages = session.scalars(select(Page).where(Page.document_id == document.id))
            errors = [f"page {page.page_no} {page.status}: {page.error}" for page in pages]
            shipment = session.get(Shipment, document.shipment_id)
            pack = shipment.reference if shipment else "?"
            report.append(f"{pack} {document.filename} {document.status}: {errors}")
    return "pipeline did not settle:\n" + "\n".join(report)


def stored(session: Session, document_id: UUID) -> tuple[Extraction, list[LineItem]]:
    extraction = session.scalars(
        select(Extraction).where(
            Extraction.document_id == document_id, Extraction.superseded_at.is_(None)
        )
    ).one()
    lines = session.scalars(
        select(LineItem).where(LineItem.extraction_id == extraction.id).order_by(LineItem.line_no)
    ).all()
    return extraction, list(lines)


def test_golden_packs_end_to_end(pipeline: dict[str, Any], wait_for: Callable[..., None]) -> None:
    uploaded = {pack: upload(pipeline, pack) for pack in PACKS}
    every_document = [doc for docs in uploaded.values() for doc in docs.values()]
    for document_id in every_document:
        classify.delay(str(document_id))
    try:
        wait_for(lambda: settled(pipeline["sessions"], every_document), timeout=180)
    except AssertionError as exc:
        with dlq.Connection(pipeline["vhost"]) as connection, dlq.dead_letters(connection) as dead:
            parked = [f"{d.stage} {d.exception}: {d.headers.get('x-manifest-error')}" for d in dead]
        report = stuck(pipeline["sessions"], every_document)
        raise AssertionError(report + "\ndead letters:\n" + "\n".join(parked)) from exc

    # Every page was answered from the cassettes; nothing was recorded or sent out.
    assert pipeline["provider"].counts == {"replayed": len(every_document)}

    with pipeline["sessions"]() as session:
        for pack, documents in uploaded.items():
            text_layer = golden.variant(pack) != "scanned"
            offline = golden.read_pack(pack, golden.cassette(pack))
            readings = {}
            shipment_ids = set()
            for stem, document_id in documents.items():
                document = session.get(Document, document_id)
                assert document is not None
                assert document.status is DocumentStatus.EXTRACTED, (pack, stem)
                assert document.kind is DocumentKind(stem)
                assert document.page_count == 1
                shipment_ids.add(document.shipment_id)

                extraction, lines = stored(session, document_id)
                assert extraction.schema_version == CURRENT_SCHEMA_VERSION
                assert extraction.model == f"{golden.PROVIDER}/{MODEL}"
                assert extraction.tokens_in
                assert extraction.tokens_out
                reading = golden.Reading(
                    extraction.payload["fields"], extraction.payload["line_items"]
                )
                # The pipeline stores exactly what the eval harness reads offline.
                assert reading == offline[stem], (pack, stem)
                readings[stem] = reading

                # Every stored value carries its confidence and provenance; no calibrator is
                # pinned here, so confidences are raw scores (zero without evidence).
                confidence, provenance = extraction.field_confidence, extraction.provenance
                assert provenance is not None
                assert extraction.calibrator is None
                assert set(confidence["fields"]) == set(provenance["fields"]) == set(reading.fields)
                assert [set(c) for c in confidence["line_items"]] == [set(r) for r in reading.lines]
                for name, read in provenance["fields"].items():
                    assert read["page"] == 1
                    assert read["signals"]["evidence"] in ("verified", "failed")
                    if read["signals"]["evidence"] == "failed":
                        assert confidence["fields"][name] == "0"

                # Line items are typed columns: Decimal money in the invoice's currency.
                assert len(lines) == len(reading.lines)
                for row, line in zip(lines, reading.lines, strict=True):
                    assert row.description == line["description"]
                    if stem == "commercial_invoice" and text_layer:
                        assert str(row.amount) == line["amount"]
                        assert row.currency == reading.fields["currency"]

            reviews = session.scalars(
                select(ReviewTask).where(ReviewTask.shipment_id.in_(shipment_ids))
            ).all()
            found = session.scalars(
                select(Discrepancy).where(Discrepancy.shipment_id.in_(shipment_ids))
            ).all()
            reasons = {reason for review in reviews for reason in review.reasons}
            reported = {(d.kind.value, d.field_key, d.line_no) for d in found}
            # Reconciliation finds what the harness finds offline...
            assert reported == evaluate_pack(pack, "test", golden.cassette(pack)).found, pack
            if golden.variant(pack) == "mismatch":
                # ...including everything the pack's label says was injected.
                assert expected_findings(golden.label(pack)) <= reported
            if golden.variant(pack) == "clean":
                # Invoice and packing list describe the same goods, line for line.
                invoice, packing = readings["commercial_invoice"], readings["packing_list"]
                alignment = align(invoice.lines, packing.lines, line_distance)
                assert alignment.pairs == tuple((i, i) for i in range(len(invoice.lines)))
            if reported:
                assert reasons & {ReviewReason.DISCREPANCY, ReviewReason.MISSING_FIELD}
            # Nothing auto-approves: no calibrator is pinned and no HS classifier is wired in,
            # so routing fails closed.
            assert {ReviewReason.LOW_CONFIDENCE, ReviewReason.HS_UNCERTAIN} <= reasons
