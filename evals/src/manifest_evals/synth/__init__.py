"""Synthetic, ground-truthed document packs (commercial invoice, packing list, bill of lading).

``python -m manifest_evals.synth`` (``make seed``) renders each pack to PDF in one of five
invoice layouts, adds "scanned" variants, and writes ground-truth JSON next to every PDF plus
a pack label with the expected cross-document discrepancies. Deterministic for a given seed.
"""
