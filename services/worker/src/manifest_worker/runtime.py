"""The adapters tasks use, built once per worker process; tests swap in their own."""

from collections.abc import Callable
from dataclasses import dataclass, field

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from manifest_db.models import EMBEDDING_DIMENSIONS
from manifest_db.session import sync_session_factory
from manifest_domain.calibration import Calibrator, load
from manifest_hs.recorded import RecordedRetriever
from manifest_hs.retrieval import PostgresRetriever, Retriever
from manifest_hs.walk import HsWalker
from manifest_llm.anthropic_provider import AnthropicProvider
from manifest_llm.cassette import CassetteProvider
from manifest_llm.embeddings import EmbeddingProvider
from manifest_llm.gateway import LlmGateway, Provider
from manifest_llm.ratelimit import Limits, TokenBucket
from manifest_llm.voyage_provider import VoyageProvider
from manifest_worker.app import WorkerSettings, settings
from manifest_worker.extraction import LlmPageExtractor
from manifest_worker.hs import LazyNotes, WalkClassifier
from manifest_worker.ports import HsClassifier, PageExtractor, UnwiredExtractor
from manifest_worker.storage import S3Storage, Storage
from manifest_worker.text import tesseract_ocr


@dataclass(frozen=True)
class Calibrators:
    """The calibrators confidences are scored with; None: raw scores, never auto-approved."""

    extraction: Calibrator | None = None
    hs: Calibrator | None = None


def load_calibrator(pin: str | None) -> Calibrator | None:
    """A committed calibrator by label (``extraction-v1``); None for no pin."""
    if not pin:
        return None
    name, _, version = pin.rpartition("-v")
    if not name or not version.isdigit():
        raise ValueError(f"calibrator {pin!r} is not <name>-v<version>")
    return load(name, int(version))


def build_calibrators(config: WorkerSettings) -> Calibrators:
    return Calibrators(
        load_calibrator(config.extraction_calibrator), load_calibrator(config.hs_calibrator)
    )


@dataclass
class Runtime:
    sessions: sessionmaker[Session]
    storage: Storage
    extractor: PageExtractor
    hs_classifier: HsClassifier | None  # None: HS classification is skipped
    ocr: Callable[[bytes], str] = tesseract_ocr
    ocr_min_chars: int = settings.ocr_min_chars
    calibrators: Calibrators = field(default_factory=Calibrators)


_current: Runtime | None = None


def build_provider(config: WorkerSettings) -> Provider | None:
    """The model to extract with: Anthropic with a key, a cassette when one is configured."""
    key = config.anthropic_api_key
    live = AnthropicProvider(api_key=key.get_secret_value()) if key else None
    if config.llm_cassettes is None:
        return live
    return CassetteProvider(
        config.llm_cassettes,
        mode=config.llm_cassette_mode,
        inner=live,
        name="anthropic",
        replay_latency=config.llm_replay_latency,
    )


def build_embedder(config: WorkerSettings) -> EmbeddingProvider | None:
    key = config.voyage_api_key
    return VoyageProvider(api_key=key.get_secret_value()) if key else None


def build_gateway(config: WorkerSettings, provider: Provider) -> LlmGateway:
    """One gateway, so extraction, HS and embeddings share the Redis rate-limit buckets."""
    limits = Limits(config.llm_requests_per_minute, config.llm_tokens_per_minute)
    embedder = build_embedder(config)
    return LlmGateway(
        {provider.name: provider},
        TokenBucket(Redis.from_url(config.redis_url)),
        default_limits=limits,
        max_inline_wait=config.llm_max_inline_wait,
        embedders={embedder.name: embedder} if embedder else None,
    )


def build_extractor(config: WorkerSettings, gateway: LlmGateway | None = None) -> PageExtractor:
    provider = build_provider(config)
    if provider is None:
        return UnwiredExtractor()
    gateway = gateway or build_gateway(config, provider)
    return LlmPageExtractor(gateway, provider.name, config.llm_model, effort=config.llm_effort)


def build_hs_classifier(
    config: WorkerSettings, sessions: sessionmaker[Session], gateway: LlmGateway | None = None
) -> HsClassifier | None:
    """Retrieval over hs_codes plus the walk; None (skipped) without a model to walk with,
    or when HS classification is turned off (``MANIFEST_HS_ENABLED=false``)."""
    provider = build_provider(config)
    if provider is None or not config.hs_enabled:
        return None
    gateway = gateway or build_gateway(config, provider)
    embedder = build_embedder(config)
    embed = None
    if embedder is not None:
        name = embedder.name

        def embed(text: str) -> list[float]:
            result = gateway.embed(
                name,
                config.embedding_model,
                [text],
                input_type="query",
                dimensions=EMBEDDING_DIMENSIONS,
            )
            return result.vectors[0]

    model = config.embedding_model if embedder else None
    retriever: Retriever = PostgresRetriever(sessions, embed, model)
    if config.hs_retrievals is not None:
        retriever = RecordedRetriever(
            config.hs_retrievals, mode=config.hs_retrieval_mode, inner=retriever
        )
    walker = HsWalker(
        gateway,
        provider.name,
        retriever,
        model=config.hs_model,
        notes=LazyNotes(sessions),
        effort=config.hs_effort,
    )
    return WalkClassifier(walker)


def build_runtime() -> Runtime:
    storage = S3Storage(
        settings.s3_endpoint_url,
        settings.s3_bucket,
        settings.s3_access_key,
        settings.s3_secret_key,
        settings.s3_region,
    )
    sessions = sync_session_factory()
    provider = build_provider(settings)
    gateway = build_gateway(settings, provider) if provider else None
    return Runtime(
        sessions,
        storage,
        build_extractor(settings, gateway),
        hs_classifier=build_hs_classifier(settings, sessions, gateway),
        calibrators=build_calibrators(settings),
    )


def runtime() -> Runtime:
    global _current
    if _current is None:
        _current = build_runtime()
    return _current


def use_runtime(replacement: Runtime | None) -> None:
    """Install ``replacement`` (tests), or None to rebuild from settings on next use."""
    global _current
    _current = replacement
