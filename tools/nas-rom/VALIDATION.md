# Validation record

Validated on November 18, 2026, with Python 3.12.14 on Linux.

## Automated verification

```sh
python3 -m unittest discover -s tests -q
python3 -m compileall -q romcatalog tests
```

All **84 tests passed**. The suite uses synthetic payloads, temporary
directories and mocked network responses; it contains no game or BIOS data.

| Area | Tests | Behavior checked |
| --- | ---: | --- |
| Auditor | 33 | XML/CMP parsing, every declared hash, renamed files, ZIP members, cache invalidation, file changes, incomplete sets, unsupported containers, symlinks, malformed inputs |
| Catalog | 23 | Real index conventions, legacy URLs, pinned revisions, DAT checksums, bounded downloads, cache corruption, provenance consistency and path containment |
| Configuration | 16 | Explicit mappings, provider selection, path handling, separate output directories and ambiguous names |
| CLI integration | 5 | Sync-to-report flow, offline audit, expected missing/unmatched outcomes, missing mounts and partial scans |
| Reports | 7 | Complete JSON/CSV exports, HTML escaping, spreadsheet formula handling and partial-run summaries |

## Live upstream compatibility

All five provider indexes were downloaded and parsed at upstream commit
`843a568f31ccd7847a3cea1f710d351119a52960`:

| Provider | DAT records in its index |
| --- | ---: |
| No-Intro | 292 |
| Redump | 60 |
| TOSEC | 2,524 |
| TOSEC-ISO | 249 |
| TOSEC-PIX | 990 |
| Total | 4,115 |

These are metadata index records, not installed games or verified ROMs.

A live `run` downloaded three real DATs, checked their byte count and CRC32,
parsed them, scanned empty test directories, and generated HTML/JSON/CSV
reports. The inputs were the No-Intro Nintendo GameCube source-code DAT,
Redump Xbox BIOS DAT, and No-Intro Game Boy DAT. Both XML and clrmamepro
formats were exercised. Empty test directories intentionally produced missing
DAT-entry reports; they were not a user's collection. The upstream modified
marker was `2023-03-24T20:20:10.073347201Z`.

Temporary upstream DAT fixtures and generated test inventories are not
included in this repository. Source attribution and byte checksums are
recorded by the utility when the user synchronizes selected metadata.

## Deployment limits

The Compose YAML and both example JSON configurations were parsed and their
mount restrictions inspected. Docker was not installed in the validation
environment, so the image was not built and a container was not run here.

No live NAS session or actual ROM collection was available. NAS installation,
share permissions, container startup and collection-scale runtime remain to
be checked on the target machine. The examples keep their mappings disabled
until the real ROM path is configured. Tests of Python behavior do not imply
that a NAS has been configured or that an emulator can launch a given game.
