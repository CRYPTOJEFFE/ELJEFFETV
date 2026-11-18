# ELJEFFETV NAS ROM catalog

Verify an existing ROM collection against the public
[videogame-archive/dat-catalog](https://github.com/videogame-archive/dat-catalog)
metadata. This utility runs beside the existing collection on a NAS, or from a
Windows, macOS, or Linux computer with the ROM share mounted. Kodi, RetroArch,
and other frontends continue to use their existing paths.

The utility downloads selected DAT metadata, reads ROM payloads, and writes
audit reports. It has no ROM download, delete, rename, repair, or move command.
The supplied Docker configuration mounts the ROM share read-only and exposes no
network ports. Reports contain collection filenames and hashes; keep the report
directory private if you do not want to share that inventory.

## Catalog version and meaning

The example configuration pins upstream commit
`843a568f31ccd7847a3cea1f710d351119a52960`, a **March 24, 2023 snapshot**. It is
a reproducible historical reference, not a live, complete list of every game.
An unmatched file is not automatically corrupt. It could be a newer dump, a
patched game, a header variant, another region, an unsupported format, or a
file outside the selected DAT's scope.

The upstream index CRC and size describe the **downloaded DAT file**. ROM
checksums are separate values read from inside that DAT. The adapter uses the
normalized catalog, handles the upstream's historical raw-file owner names,
and checks downloaded DAT integrity before use. DAT files are fetched at run
time and are not bundled in this repository. Provider terms remain separate
from this utility's GPL-2.0-or-later source license.

## Quick start on a UGREEN NAS with Docker Compose

To obtain just this utility from the feature branch without downloading the
Kodi source history, use a sparse checkout on a computer with Git:

```sh
git clone --depth 1 --filter=blob:none --sparse --branch feat/nas-rom-dat-catalog https://github.com/CRYPTOJEFFE/ELJEFFETV.git ELJEFFETV-tools
cd ELJEFFETV-tools
git sparse-checkout set tools/nas-rom
cd tools/nas-rom
```

Run commands from this `tools/nas-rom` directory on the NAS. A current Docker
Compose plugin and access to the Docker daemon are prerequisites. This is a
one-shot job: it writes a report and exits; it is not a continuously running
server.

1. Identify the **actual filesystem path on the NAS** for the existing ROM
   share. A share named `roms` does not establish whether its path starts with
   `/volume1`, `/volume2`, or something else. Use the NAS file manager or an
   existing verified mount configuration. Do not create a new empty ROM folder
   to satisfy a guessed path.

2. Put this utility in a separate application directory, outside the ROM
   collection. Create a report directory there using the NAS account that will
   run it. Copy the templates:

   ```sh
   cp .env.example .env
   cp examples/config.json config.json
   mkdir -p reports
   id -u
   id -g
   ```

3. Edit `.env`. Set `ROM_ROOT` to the verified absolute ROM directory and
   `REPORT_DIR` to the absolute path of the newly created `reports` directory.
   Set `NAS_UID` and `NAS_GID` to the numeric values returned above. The account
   must already have read access to the collection and write access to the
   reports. Set the IDs before the first build so the named cache volume has
   the matching owner. No administrator-wide permission changes are needed.

4. Build and find the exact DAT name for a platform:

   ```sh
   docker compose config
   docker compose build
   docker compose run --rm rom-catalog catalog --provider No-Intro --query "Game Boy"
   ```

5. Edit `config.json`. In the example mapping, replace `rom_dir` with the
   existing platform directory **inside the container** (`/roms/` followed by
   its path relative to `ROM_ROOT`). The sample already selects the pinned
   Game Boy DAT; for another system, copy the exact DAT filename returned by
   `catalog` into `dat_name`. Set `enabled` to `true`. Duplicate the mapping for
   each additional platform, giving each a distinct `name`. The disabled
   placeholder is deliberately not a working mapping.

6. Validate the paths, then download the selected metadata and audit:

   ```sh
   docker compose run --rm rom-catalog doctor --config /config/config.json
   docker compose run --rm rom-catalog
   ```

7. Open `index.html` from `REPORT_DIR`. JSON and CSV outputs are
   provided for a collection dashboard, spreadsheet import, or later inventory
   comparisons. Review scan errors and unsupported files before interpreting
   missing entries.

If a provider has more than one DAT with the same filename, selection fails
instead of guessing. **Replace `dat_name` with `dat_path`** in that mapping;
exactly one selector is allowed. Set `dat_path` to the exact full
`root/normalized/...` path returned by `catalog`. An example for the pinned
Game Boy DAT is:

```json
"dat_path": "root/normalized/No-Intro/No-Intro/Nintendo - Game Boy/Nintendo - Game Boy (20230318-005235).dat"
```

The Compose job uses one CPU and a 1 GiB memory limit as a starting point. A
large selected DAT may require a larger memory limit; an out-of-memory exit is
an incomplete run, never proof of an empty collection. The first scan must read
the ROM payloads and may take time on a large NAS. Subsequent scans can reuse
unchanged-file checksum cache entries.

### Repeat an audit without downloading metadata

```sh
docker compose run --rm rom-catalog audit --config /config/config.json
```

Force payloads to be read again instead of trusting cached unchanged-file
hashes:

```sh
docker compose run --rm rom-catalog audit --config /config/config.json --rehash
```

For an integrity check after storage errors or an untrusted copy, use
`--rehash`. Cached results are a performance feature, not a fresh read of every
byte. If setting up a NAS scheduler later, run the same command only after one
manual audit succeeds, keep its logs, and avoid overlapping runs.

## Run from Windows, macOS, or Linux without Docker

Python 3.11 or later is required; the utility uses the standard library and
does not require `pip install`. Run it from this directory. Mount the existing
NAS share using the operating system's normal authenticated SMB connection.

Copy `examples/config.local.json` to `config.local.json`, then replace the
mapping placeholders and enable that mapping as described above. Set
`rom_dir` to the mounted platform directory. On Windows, JSON paths can use
forward slashes, for example `Z:/existing-platform-folder`; a UNC path needs
escaped backslashes in JSON. Mac paths usually start with `/Volumes/`, but the
actual mount name must be checked. Keep cache and report directories outside
the ROM root.

```sh
python3 -m romcatalog catalog --provider No-Intro --query "Game Boy"
python3 -m romcatalog doctor --config config.local.json
python3 -m romcatalog run --config config.local.json
```

In Windows PowerShell, replace `python3` with `py -3` if that is how Python is
installed. Reading over SMB does not require copying the whole ROM library to
the client computer. Running directly on the NAS usually avoids transporting
every payload across the network for the first scan.

## Commands

| Command | Purpose |
| --- | --- |
| `catalog --provider No-Intro --query "Game Boy"` | Discover exact available DAT names for a selected provider. |
| `catalog --provider Redump --query "PlayStation" --json` | Return matching catalog metadata as JSON. |
| `doctor --config config.json` | Check configuration and mapped paths before scanning. |
| `sync --config config.json` | Fetch and verify only the DATs selected in enabled mappings. |
| `audit --config config.json` | Compare payloads with already cached DATs and generate reports. |
| `run --config config.json` | Synchronize metadata, then perform the audit. |
| `audit --config config.json --rehash` | Read payloads again instead of reusing unchanged-file hashes. |

`catalog` uses the pinned revision by default. If you intentionally change
`source.ref` in the config, pass that same revision as `catalog --ref ...`
when discovering DAT names for it. Relative paths in a native config are
resolved relative to the config file's directory.

## Interpret results correctly

`summary.json` and `index.html` describe the complete run. Each mapping also
has a JSON report and separate CSV files named with the mapping name followed
by `-matched`, `-missing`, `-unmatched`, `-issues`, `-sets`, or `-duplicates`.
An exit code of `0` means the requested scan completed, even if files are
unmatched or DAT entries are missing. Exit code `2` means a configuration,
network, or DAT error. Exit code `3` means a partial scan, such as unsupported
or unreadable files; examine the issues before treating absence as conclusive.

An audit is scoped to the explicit combination of a ROM folder and a DAT. Do
not map the whole multi-platform library to a single platform DAT. Providers
may organize the same system differently; choose the record matching the
stored format and collection scope.

- **Verified:** size and supported checksums agree with a DAT ROM entry.
  A CRC-only match has less assurance than a SHA-1, MD5, or SHA-256 match.
- **Unmatched:** readable bytes did not match the selected DAT. This is not a
  corruption diagnosis and is not a deletion recommendation.
- **Missing DAT entries:** expected ROM entries were not verified within that
  mapped folder. Regions, revisions, demos, tracks, clone sets, and related
  variants can have separate entries. This is not a unique missing-game count.
- **Incomplete sets:** a multi-ROM set has only some of its expected entries
  verified. A multi-track disc can require several individual files.
- **Unsupported or error:** the utility could not fully verify the payload.
  Treat those results as unresolved when assessing collection coverage.
- **Duplicates:** repeated strong payload hashes identify matching bytes.
  The report does not remove copies or assume they are unnecessary to a
  frontend's folder structure.

Uncompressed files and ordinary ZIP members are checked without extracting
archives into the ROM share. Specialized containers such as CHD, RVZ, CSO,
7z, and RAR need a separate format-aware workflow; their container hash is not
the raw ROM or track hash from a DAT. Headered versus headerless dumps also
need matching source metadata. No header stripping, disc reconstruction,
archive conversion, or emulator compatibility check is implied.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Compose says `ROM_ROOT` or `REPORT_DIR` is missing | Complete `.env` with existing absolute NAS paths. |
| Bind mount does not exist | Verify the NAS filesystem path; the configuration intentionally does not create source paths. |
| Permission denied on ROMs | Check that `NAS_UID`/`NAS_GID` identify an account already allowed to read that share. |
| Permission denied on reports/cache | Check report ownership and that the named volume was first created with the same numeric account. A later UID change does not automatically change existing volume ownership. |
| No enabled mapping | Replace the sample mapping placeholders and set `enabled: true`. |
| DAT not found | Copy the exact name from `catalog` for the same provider and source revision. |
| Unexpectedly large missing count | Check platform, provider, region/revision scope, payload format, and scan completeness. |
| Old catalog contents | This source is pinned to a March 2023 snapshot. Updating the code does not create newer upstream DAT data. |
| Interrupted, failed, or out-of-memory scan | Keep the error/log and repeat after resolving it. Do not interpret an incomplete scan as verified absence. |

The provided configuration does not enable SSH, change NAS share permissions,
mount cloud accounts, or modify a frontend. It needs an existing local or
mounted path. Installing it in GitHub does not install it on a NAS.

## References

- [Upstream DAT catalog and index format](https://github.com/videogame-archive/dat-catalog)
- [Pinned source revision](https://github.com/videogame-archive/dat-catalog/commit/843a568f31ccd7847a3cea1f710d351119a52960)
- [Docker Compose service and volume options](https://docs.docker.com/reference/compose-file/services/)
- [Docker Compose variable interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)
