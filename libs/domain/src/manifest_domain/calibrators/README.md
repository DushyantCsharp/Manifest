# Calibrators

Versioned isotonic calibrators, `<name>-v<N>.json` (`extraction`, `hs`), written by
`make calibrate` from an eval run and loaded by `manifest_domain.calibration.load`. Build
outputs: never hand-edit; refit and commit. `make eval` fails when a committed calibrator no
longer matches a refit on the committed cassettes.

`provenance: oracle` means the answers came from the ground-truth oracle that seeds cassettes
offline. Such a calibrator is never trusted for routing: nothing auto-approves on it.
