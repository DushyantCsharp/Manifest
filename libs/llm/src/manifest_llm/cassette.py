"""Record and replay provider responses, keyed by a hash of the request (vcrpy-style).

A cassette is a directory holding one JSON recording per distinct request, named by the
request's SHA-256 key. The key covers everything that can change the answer: provider, model,
system prompt, prompt, output schema, token budget and (when set) effort. Runs of whitespace in
the prompts collapse first, so text that differs only in spacing still hits. A recording keeps
the live call's latency, which replays report, so evals measure the model and not the replay.

``CassetteProvider`` wraps a real provider and sits behind the gateway like any other, so
replayed calls still pass the token bucket and output validation. Modes:

- ``replay``: answer from the cassette. A miss raises ``CassetteMissError`` with a diff
  against the closest recording; nothing is ever sent to a provider.
- ``record``: call the wrapped provider and (over)write the recording.
- ``new``: replay what is recorded, record what isn't.
"""

import difflib
import hashlib
import json
import tempfile
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from manifest_llm.errors import PermanentProviderError
from manifest_llm.gateway import JsonRequest, JsonResponse, Provider

Mode = Literal["replay", "record", "new"]
FORMAT = 1


def _collapse(text: str) -> str:
    return " ".join(text.split())


def request_key(provider: str, request: JsonRequest) -> str:
    canonical: dict[str, Any] = {
        "provider": provider,
        "model": request.model,
        "system": _collapse(request.system),
        "prompt": _collapse(request.prompt),
        "schema": request.schema,
        "max_tokens": request.max_tokens,
    }
    if request.effort is not None:  # absent when unset, so older recordings keep their keys
        canonical["effort"] = request.effort
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class CassetteMissError(PermanentProviderError):
    """Replay found no recording for this request (re-record with a live provider)."""


class CassetteProvider:
    def __init__(
        self,
        directory: Path,
        *,
        mode: Mode = "replay",
        inner: Provider | None = None,
        name: str | None = None,
        recorded_with: str | None = None,
    ) -> None:
        """``name`` is the provider name requests are keyed and routed by (default: the
        wrapped provider's). ``recorded_with`` notes what really answered, when that isn't
        the named provider (a stand-in that answers from ground truth, say).
        """
        if mode != "replay" and inner is None:
            raise ValueError(f"mode {mode!r} needs a provider to record from")
        resolved = name or (inner.name if inner is not None else None)
        if resolved is None:
            raise ValueError("name the provider being replayed")
        self.directory = directory
        self.mode = mode
        self.name = resolved
        self._inner = inner
        self._recorded_with = recorded_with or (inner.name if inner is not None else None)
        self._lock = threading.Lock()
        self.counts: Counter[str] = Counter()  # replayed / recorded
        self.sources: Counter[str] = Counter()  # who answered what was replayed (recorded_with)

    def path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def complete_json(self, request: JsonRequest) -> JsonResponse:
        key = request_key(self.name, request)
        path = self.path(key)
        if self.mode != "record" and path.exists():
            recording = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
            self._count("replayed", str(recording.get("recorded_with") or self.name))
            response = recording["response"]
            return JsonResponse(
                response["data"],
                response["input_tokens"],
                response["output_tokens"],
                response.get("latency_ms"),
            )
        if self.mode == "replay" or self._inner is None:
            raise CassetteMissError(self._miss(key, request))
        started = time.monotonic()
        response = self._inner.complete_json(request)
        latency_ms = response.latency_ms
        if latency_ms is None:
            latency_ms = int((time.monotonic() - started) * 1000)
        self._write(key, request, replace(response, latency_ms=latency_ms))
        self._count("recorded")
        return response

    def _count(self, outcome: str, source: str | None = None) -> None:
        with self._lock:
            self.counts[outcome] += 1
            if source is not None:
                self.sources[source] += 1

    def _write(self, key: str, request: JsonRequest, response: JsonResponse) -> None:
        recording: dict[str, Any] = {
            "format": FORMAT,
            "key": key,
            "provider": self.name,
            "recorded_with": self._recorded_with,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": {
                "model": request.model,
                "max_tokens": request.max_tokens,
                "system_sha256": hashlib.sha256(request.system.encode()).hexdigest(),
                "schema_sha256": hashlib.sha256(
                    json.dumps(request.schema, sort_keys=True).encode()
                ).hexdigest(),
                "prompt": request.prompt,
            },
            "response": {
                "data": response.data,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
            },
        }
        if request.effort is not None:
            recording["request"]["effort"] = request.effort
        self.directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(recording, indent=2, sort_keys=True, ensure_ascii=False, default=str)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.directory, suffix=".tmp", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text + "\n")
        Path(handle.name).replace(self.path(key))

    def _miss(self, key: str, request: JsonRequest) -> str:
        message = (
            f"no recording {key[:12]} for {self.name}/{request.model} in {self.directory}; "
            "re-record with a live provider (make test-live)"
        )
        closest = self._closest(request)
        if closest is None:
            return message
        name, prompt = closest
        diff = difflib.unified_diff(
            prompt.splitlines(), request.prompt.splitlines(), name, "this request", lineterm="", n=1
        )
        return message + "\nclosest recording's prompt differs:\n" + "\n".join(list(diff)[:20])

    def _closest(self, request: JsonRequest) -> tuple[str, str] | None:
        best: tuple[float, str, str] | None = None
        for path in sorted(self.directory.glob("*.json")):
            recorded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["request"]
            if recorded.get("model") != request.model:
                continue
            prompt = str(recorded.get("prompt", ""))
            ratio = difflib.SequenceMatcher(None, prompt, request.prompt).quick_ratio()
            if best is None or ratio > best[0]:
                best = (ratio, path.name, prompt)
        return None if best is None else (best[1], best[2])
