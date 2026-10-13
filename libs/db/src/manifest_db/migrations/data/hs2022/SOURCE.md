# HS 2022 nomenclature (vendored)

`harmonized-system.csv` is loaded by migration `0002_hs2022_nomenclature`, which checks its
sha256. Vendored so migrations never touch the network and always load the same data.

- Source: https://github.com/datasets/harmonized-system, `data/harmonized-system.csv`
- Commit: `b1be3f2531a85fb0ea17147b19c41d3641912e43` (datapackage version 2022.0)
- Upstream: UN Comtrade HS 2022 classification
- Licence: ODC-PDDL-1.0 (public domain dedication)
- sha256: `e77ee9e1cfd15a42d1fadeb2c0618d10f6b9ed9aec0e5d66826dc7ef9d3126a5`
- Columns: `section, hscode, description, parent, level`

Contents: 97 chapters (level 2), 1,229 headings (level 4) and 5,613 subheadings (level 6), plus
one `TOTAL` pseudo-row that the migration skips. Chapters have parent `TOTAL`, stored as NULL.

Known limitation: UN Comtrade truncates descriptions at 255 characters; 17 rows are cut off
(e.g. `293333`). Retrieval should tolerate that; don't "fix" the file by hand, because the
checksum pins it.

Never edit this file. A new nomenclature edition is a new file and a new migration.
