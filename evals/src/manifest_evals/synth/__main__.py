"""Synthetic document packs.

python -m manifest_evals.synth          regenerate the syn-* packs (``make seed``)
python -m manifest_evals.synth freeze   rewrite the committed eval corpus (``make corpus``)
"""

import argparse
import sys
import time
from pathlib import Path

from manifest_evals.harness import EVALS_ROOT
from manifest_evals.synth.corpus import freeze
from manifest_evals.synth.generate import build_packs
from manifest_evals.synth.output import clear, write_pack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m manifest_evals.synth",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=["generate", "freeze"], default="generate")
    parser.add_argument("--packs", type=int, default=40)
    parser.add_argument("--mismatched", type=int, default=15, help="packs with mismatches")
    parser.add_argument("--seed", type=int, default=2022)
    parser.add_argument("--out", type=Path, default=EVALS_ROOT, help="contains golden/, labels/")
    parser.add_argument("--dpi", type=int, default=150, help="resolution of scanned variants")
    parser.add_argument("--no-scans", action="store_true", help="skip the scanned variants")
    args = parser.parse_args(argv)

    golden, labels = args.out / "golden", args.out / "labels"
    started = time.monotonic()
    if args.command == "freeze":
        corpus = freeze(args.out, dpi=args.dpi)
        elapsed = time.monotonic() - started
        print(f"froze {len(corpus)} packs in {elapsed:.0f}s under {args.out}")
        return 0
    packs = build_packs(args.packs, args.mismatched, args.seed)
    clear(golden, labels)
    for pack in packs:
        write_pack(pack, golden, labels, scans=not args.no_scans, dpi=args.dpi)
        print(f"{pack.pack_id} {pack.layout:<10} {len(pack.mismatches)} mismatch(es)", flush=True)

    mismatched = sum(1 for pack in packs if pack.mismatches)
    elapsed = time.monotonic() - started
    print(f"wrote {len(packs)} packs ({mismatched} with mismatches) in {elapsed:.0f}s to {golden}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
