"""``python -m manifest_evals``: the eval harness.

python -m manifest_evals                 check every pack has a label and vice versa
python -m manifest_evals run             replay the corpus, report, gate (make eval)
python -m manifest_evals calibrate       ... and commit a refitted calibrator
python -m manifest_evals run --mode record   re-record every cassette from a real model
"""

import argparse
import sys
from pathlib import Path

from manifest_evals.harness import EVALS_ROOT, discover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manifest_evals",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", nargs="?", choices=["discover", "run", "calibrate"], default="discover"
    )
    parser.add_argument("--golden", type=Path, default=EVALS_ROOT / "golden")
    parser.add_argument("--labels", type=Path, default=EVALS_ROOT / "labels")
    parser.add_argument("--mode", choices=["replay", "record", "live"], default="replay")
    parser.add_argument(
        "--pack", action="append", default=[], help="only these packs (no gate on drift)"
    )
    parser.add_argument("--report", type=Path, default=EVALS_ROOT / "reports" / "latest")
    args = parser.parse_args(argv)

    if args.command != "discover":
        from manifest_evals import run  # the harness pulls in the pipeline; discovery doesn't

        return run.main(args.command, args.mode, args.pack, args.report)

    found = discover(args.golden, args.labels)
    for problem in found.problems:
        print(f"error: {problem}", file=sys.stderr)
    print(f"{len(found.packs)} golden pack(s) discovered")
    return 1 if found.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
