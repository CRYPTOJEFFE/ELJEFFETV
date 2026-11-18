"""Command-line entry point: python -m romcatalog --help."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .catalog import CatalogError, list_catalog, load_manifest, resolve_ref, sync_catalog
from .config import ConfigError, PROVIDERS, SNAPSHOT, load_config, mapping_identity
from .report import save_reports


def doctor(config: dict) -> tuple[dict, bool]:
    checks = []
    for mapping in config["mappings"]:
        path = mapping["rom_dir"]
        accessible = path.is_dir() and os.access(path, os.R_OK | os.X_OK)
        checks.append({"kind": "rom_directory", "name": mapping["name"], "path": str(path),
                       "ok": accessible, "message": "Readable directory" if accessible else "Missing or unreadable directory"})
    for key in ("cache_dir", "state_dir", "report_dir"):
        path = config[key]
        parent = path
        while not parent.exists() and parent.parent != parent:
            parent = parent.parent
        okay = parent.is_dir() and os.access(parent, os.W_OK | os.X_OK) and (not path.exists() or path.is_dir())
        checks.append({"kind": key, "path": str(path), "ok": okay,
                       "message": "Writable parent available" if okay else "Directory or writable parent unavailable"})
    return {"checks": checks, "source": config["source"],
            "note": "Read-only checks; no ROM bytes read. This does not test Docker or confirm NAS network reachability."}, all(c["ok"] for c in checks)


def audit(config: dict, rehash: bool = False) -> dict:
    from .audit import audit_library
    manifest = load_manifest(config)
    # Fail before producing misleading missing results if a whole mapping is unavailable.
    for mapping in config["mappings"]:
        if not mapping["rom_dir"].is_dir():
            raise ConfigError(f"ROM directory is not mounted or does not exist: {mapping['rom_dir']}")
    results = []
    for mapping, saved in zip(config["mappings"], manifest["mappings"]):
        print(f"Auditing {mapping['name']}: {mapping['rom_dir']}", file=sys.stderr, flush=True)
        result = audit_library(mapping["rom_dir"], Path(saved["local_path"]),
                               cache_path=config["state_dir"] / f"{mapping['name']}.sqlite3", rehash=rehash)
        result["mapping"] = mapping_identity(mapping)
        result["catalog_source"] = {**manifest["source"], "dat": saved["record"], "sha256": saved["sha256"]}
        results.append(result)
    return save_reports(config["report_dir"], results, manifest["source"])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="ELJEFFETV: verify a NAS ROM library against a pinned DAT catalog")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    catalog = sub.add_parser("catalog", help="Search one provider's DAT metadata (no ROM downloads)")
    catalog.add_argument("--provider", choices=PROVIDERS, required=True)
    catalog.add_argument("--query", default="")
    catalog.add_argument("--ref", default=SNAPSHOT, help="Pinned commit (default) or main, resolved once")
    catalog.add_argument("--json", action="store_true", help="Print machine-readable catalog records")
    for command in ("sync", "audit", "run", "doctor"):
        item = sub.add_parser(command, help={"sync": "Download and verify mapped DAT files only",
                                             "audit": "Audit ROMs against cached DATs; works offline",
                                             "run": "Sync DATs, then audit mapped ROM folders",
                                             "doctor": "Check configured paths without writing"}[command])
        item.add_argument("--config", required=True, type=Path)
        if command in ("audit", "run"):
            item.add_argument("--rehash", action="store_true", help="Read ROM bytes again instead of reusing cached hashes")
    return root


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "catalog":
            commit = resolve_ref(arguments.ref)
            records = [r for r in list_catalog(arguments.provider, commit)
                       if arguments.query.casefold() in (r["dat_name"] + " " + r["dat_path"]).casefold()]
            if arguments.json:
                print(json.dumps(records, indent=2, ensure_ascii=False))
            else:
                print(f"{len(records)} DAT files · {arguments.provider} · commit {commit}")
                for record in records:
                    print(f"\n{record['dat_name']}\n  dat_path: {record['dat_path']}\n  DAT bytes: {record['size']:,}")
            return 0
        config = load_config(arguments.config)
        if arguments.command == "doctor":
            report, okay = doctor(config)
            print(json.dumps(report, indent=2))
            return 0 if okay else 2
        if arguments.command in ("sync", "run"):
            manifest = sync_catalog(config)
            print(f"Verified {len(manifest['mappings'])} DAT files; snapshot {manifest['source']['catalog_modified']}",
                  file=sys.stderr)
            if arguments.command == "sync":
                print(json.dumps(manifest, indent=2, ensure_ascii=False))
                return 0
        summary = audit(config, arguments.rehash)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"Reports: {config['report_dir'] / 'index.html'}", file=sys.stderr)
        return 0 if summary["complete_scan"] else 3
    except (ConfigError, CatalogError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; ROM files were not changed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
