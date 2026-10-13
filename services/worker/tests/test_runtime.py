"""Which model the worker extracts with, from settings alone (nothing connects at build time)."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from manifest_llm.anthropic_provider import AnthropicProvider
from manifest_llm.cassette import CassetteProvider
from manifest_worker.app import WorkerSettings
from manifest_worker.extraction import LlmPageExtractor
from manifest_worker.ports import UnwiredExtractor
from manifest_worker.runtime import (
    Calibrators,
    build_calibrators,
    build_extractor,
    build_provider,
    load_calibrator,
)


def config(**values: object) -> WorkerSettings:
    return WorkerSettings.model_validate({"ANTHROPIC_API_KEY": None, **values})


def test_without_a_key_or_cassette_extraction_is_unwired() -> None:
    assert isinstance(build_extractor(config()), UnwiredExtractor)


def test_a_key_extracts_with_anthropic() -> None:
    settings = config(ANTHROPIC_API_KEY=SecretStr("sk-test"), llm_model="claude-x")
    assert isinstance(build_provider(settings), AnthropicProvider)
    extractor = build_extractor(settings)
    assert isinstance(extractor, LlmPageExtractor)
    assert (extractor.provider, extractor.model) == ("anthropic", "claude-x")


def test_a_cassette_replays_without_a_key(tmp_path: Path) -> None:
    provider = build_provider(config(llm_cassettes=tmp_path))
    assert isinstance(provider, CassetteProvider)
    assert (provider.name, provider.mode, provider.directory) == ("anthropic", "replay", tmp_path)


def test_recording_needs_a_live_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a provider"):
        build_provider(config(llm_cassettes=tmp_path, llm_cassette_mode="record"))
    recording = build_provider(
        config(
            llm_cassettes=tmp_path, llm_cassette_mode="new", ANTHROPIC_API_KEY=SecretStr("sk-test")
        )
    )
    assert isinstance(recording, CassetteProvider)
    assert recording.mode == "new"


def test_extraction_effort_comes_from_settings() -> None:
    settings = config(ANTHROPIC_API_KEY=SecretStr("sk-test"), llm_effort="medium")
    extractor = build_extractor(settings)
    assert isinstance(extractor, LlmPageExtractor)
    assert extractor.effort == "medium"


def test_no_calibrators_are_pinned_by_default() -> None:
    assert build_calibrators(config()) == Calibrators(None, None)


def test_a_pinned_calibrator_must_exist_and_be_well_named(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="<name>-v<version>"):
        load_calibrator("extraction")
    with pytest.raises(FileNotFoundError):
        load_calibrator("extraction-v999")
    assert load_calibrator(None) is None
