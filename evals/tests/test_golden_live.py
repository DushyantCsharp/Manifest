"""A real model reads the golden packs (``make test-live``; needs ANTHROPIC_API_KEY).

Excluded by default (``-m live``). Recordings go to a temporary folder unless
MANIFEST_RECORD_CASSETTES=1, which replaces the committed cassettes in ``evals/cassettes``:
review that diff before committing it, since replayed tests then assert what the model read.
"""

import os
from pathlib import Path

import pytest

from manifest_evals import golden

pytestmark = pytest.mark.live

# Of values per document; misses are listed in the failure. OCR damage is the model's to
# recover from where it can, so scans get a lower floor.
MIN_ACCURACY = {"clean": 0.95, "mismatch": 0.95, "scanned": 0.85}


@pytest.fixture(scope="module", autouse=True)
def api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("live tests need ANTHROPIC_API_KEY")


@pytest.mark.parametrize("pack", golden.golden_packs())
def test_a_live_model_reads_the_golden_pack(pack: str, tmp_path: Path) -> None:
    commit = os.environ.get("MANIFEST_RECORD_CASSETTES") == "1"
    root = golden.CASSETTES if commit else tmp_path
    golden.record(pack, root=root)

    readings = golden.read_pack(pack, golden.cassette(pack, root=root))  # replays what it recorded
    for document, truth in golden.truths(pack).items():
        right, total = golden.accuracy(truth, readings[document])
        misses = golden.differences(truth, readings[document])
        floor = MIN_ACCURACY[golden.variant(pack)]
        assert right / total >= floor, f"{pack} {document}: {right}/{total}\n" + "\n".join(misses)
