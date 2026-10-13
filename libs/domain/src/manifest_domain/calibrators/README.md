# Calibrators

Versioned isotonic calibrators, `<name>-v<N>.json` (`extraction`, `hs`), written by
`make calibrate` from an eval run and loaded by `manifest_domain.calibration.load`. Build
outputs: never hand-edit; refit and commit. `make eval` fails when a committed calibrator no
longer matches a refit on the committed cassettes.

`provenance: oracle` means the answers came from the ground-truth oracle that seeds cassettes
offline. Such a calibrator is never trusted for routing: nothing auto-approves on it.

| version | fitted on |
|---|---|
| `extraction-v1`, `hs-v1` | oracle answers and offline BM25 retrieval (`provenance: oracle`) |
| `extraction-v2`, `hs-v2` | the first real recording, 2026-10-13: Sonnet 5.5 extraction and HS walk, voyage-3.5 + FTS retrieval (`provenance: model`) |
