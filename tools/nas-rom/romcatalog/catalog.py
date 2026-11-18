"""Fetch historical catalog metadata and verify each DAT before installing it."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .config import PROVIDERS, REPOSITORY, SNAPSHOT, mapping_identity

MAX_INDEX = 32 * 1024**2
MAX_DAT = 512 * 1024**2
ALLOWED_HOSTS = {"raw.githubusercontent.com", "api.github.com"}
ALLOWED_OWNERS = {"videogame-archive", "open-retrogaming-archive"}


class CatalogError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".romcatalog-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _check_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise CatalogError("Catalog downloads must use HTTPS on the approved GitHub hosts")


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, limit: int) -> bytes:
    _check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "ELJEFFETV-ROM-Catalog/1.0",
                                                  "Accept": "application/vnd.github+json" if "api.github.com" in url else "*/*"})
    try:
        with urllib.request.build_opener(TrustedRedirect()).open(request, timeout=45) as response:
            _check_url(response.url)
            chunks, total = [], 0
            while True:
                chunk = response.read(min(1024 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise CatalogError(f"Download exceeds {limit} bytes: {url}")
            return b"".join(chunks)
    except (OSError, urllib.error.URLError) as exc:
        raise CatalogError(f"Cannot download {url}: {exc}") from exc


def resolve_ref(ref: str) -> str:
    if re.fullmatch(r"[a-fA-F0-9]{40}", ref):
        return ref.lower()
    if ref != "main":
        raise CatalogError("Use main or a full commit SHA")
    try:
        payload = json.loads(download(f"https://api.github.com/repos/{REPOSITORY}/git/ref/heads/main", 1024**2))
        sha = payload["object"]["sha"]
        if payload["object"]["type"] != "commit" or not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("not a commit")
        return sha
    except (ValueError, KeyError, TypeError) as exc:
        raise CatalogError("GitHub returned an invalid main branch reference") from exc


def raw_url(commit: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/" + urllib.parse.quote(path, safe="/")


def parse_index(data: bytes, provider: str, commit: str) -> list[dict]:
    if provider not in PROVIDERS:
        raise CatalogError(f"Unknown provider: {provider}")
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != ["Type", "Name", "URL", "CRC", "Size"]:
            raise CatalogError("Unexpected catalog index columns")
        records = []
        seen = set()
        for row in reader:
            if row.get("Type") == "DIRECTORY":
                continue  # Project index is already a flattened recursive listing.
            if row.get("Type") != "FILE":
                raise CatalogError("Unexpected catalog index row type")
            if not (row.get("Name") or "").lower().endswith(".dat"):
                continue  # Upstream also indexes modified markers and supplemental resources.
            url = urllib.parse.urlsplit(row["URL"])
            segments = urllib.parse.unquote(url.path).lstrip("/").split("/")
            if (url.scheme != "https" or url.netloc != "raw.githubusercontent.com"
                    or len(segments) < 7 or segments[0] not in ALLOWED_OWNERS
                    or segments[1] != "dat-catalog" or segments[3:6] != ["root", "normalized", provider]
                    or url.query or url.fragment or any(part in ("", ".", "..") for part in segments)
                    or any("\\" in part or "\x00" in part for part in segments)):
                raise CatalogError("Index contains an unsafe or unexpected DAT URL")
            path = "/".join(segments[3:])
            if PurePosixPath(path).name != row["Name"] or not path.lower().endswith(".dat"):
                raise CatalogError("DAT filename does not match its catalog path")
            crc, size = int(row["CRC"]), int(row["Size"])
            if not 0 <= crc <= 0xFFFFFFFF or not 0 < size <= MAX_DAT:
                raise CatalogError("Invalid DAT CRC or size in catalog")
            if path in seen:
                raise CatalogError(f"Duplicate catalog path: {path}")
            seen.add(path)
            records.append({"provider": provider, "dat_name": row["Name"], "dat_path": path,
                            "size": size, "dat_crc32": f"{crc:08x}", "source_commit": commit,
                            "url": raw_url(commit, path)})
        if not records:
            raise CatalogError(f"No DAT files in {provider} index")
        return records
    except (UnicodeError, ValueError, KeyError, TypeError, csv.Error) as exc:
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError(f"Invalid {provider} index: {exc}") from exc


def list_catalog(provider: str, commit: str) -> list[dict]:
    path = f"root/normalized/{provider}/index.csv"
    return parse_index(download(raw_url(commit, path), MAX_INDEX), provider, commit)


def select_record(records: list[dict], mapping: dict) -> dict:
    key = "dat_path" if "dat_path" in mapping else "dat_name"
    matches = [item for item in records if item[key] == mapping[key]]
    if len(matches) != 1:
        raise CatalogError(f"{mapping['name']}: found {len(matches)} DATs matching {key}={mapping[key]!r}. "
                           "Run catalog and copy an exact unique dat_name (or dat_path).")
    return matches[0]


def verify_dat(data: bytes, record: dict) -> str:
    if len(data) != record["size"] or f"{zlib.crc32(data) & 0xFFFFFFFF:08x}" != record["dat_crc32"]:
        raise CatalogError(f"DAT download failed size/CRC verification: {record['dat_name']}")
    return hashlib.sha256(data).hexdigest()


def sync_catalog(config: dict) -> dict:
    commit = resolve_ref(config["source"]["ref"])
    catalogs = {provider: list_catalog(provider, commit)
                for provider in dict.fromkeys(item["provider"] for item in config["mappings"])}
    modified = download(raw_url(commit, "root/normalized/modified"), 4096).decode("utf-8").strip()
    # Gather every selected DAT before publishing a new usable manifest.
    manifest = {"schema_version": 1, "synced_at": now(), "source": {
        "repository": REPOSITORY, "requested_ref": config["source"]["ref"], "commit": commit,
        "catalog_modified": modified, "url": f"https://github.com/{REPOSITORY}/tree/{commit}"}, "mappings": []}
    for mapping in config["mappings"]:
        record = select_record(catalogs[mapping["provider"]], mapping)
        key = hashlib.sha256(record["dat_path"].encode()).hexdigest()[:24]
        local = config["cache_dir"] / commit / record["provider"] / f"{key}.dat"
        if local.is_symlink() or not local.resolve().is_relative_to(config["cache_dir"]):
            raise CatalogError("DAT cache path contains a symlink outside the configured cache")
        cached = False
        if local.is_file():
            data = local.read_bytes()
            try:
                digest = verify_dat(data, record)
                cached = True
            except CatalogError:
                pass
        if not cached:
            data = download(record["url"], record["size"])
            digest = verify_dat(data, record)
            atomic_write(local, data)
        manifest["mappings"].append({"mapping": mapping_identity(mapping), "record": record,
                                      "local_path": str(local), "sha256": digest})
    write_json(config["cache_dir"] / "manifest.json", manifest)
    return manifest


def load_manifest(config: dict) -> dict:
    try:
        value = json.loads((config["cache_dir"] / "manifest.json").read_text(encoding="utf-8"))
        if value["schema_version"] != 1 or value["source"]["repository"] != REPOSITORY:
            raise ValueError("wrong manifest format/source")
        if value["source"]["requested_ref"] != config["source"]["ref"]:
            raise ValueError("configured source changed")
        commit = value["source"]["commit"]
        if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("invalid source commit")
        if config["source"]["ref"] != "main" and commit != config["source"]["ref"]:
            raise ValueError("cached source no longer matches the pinned commit")
        if [entry["mapping"] for entry in value["mappings"]] != [mapping_identity(m) for m in config["mappings"]]:
            raise ValueError("configured mappings changed")
        for entry in value["mappings"]:
            mapping, record = entry["mapping"], entry["record"]
            selector = "dat_path" if "dat_path" in mapping else "dat_name"
            parts = PurePosixPath(record["dat_path"]).parts
            if (record["provider"] != mapping["provider"] or record[selector] != mapping[selector]
                    or record["source_commit"] != commit
                    or parts[:3] != ("root", "normalized", mapping["provider"])
                    or any(part in (".", "..") for part in parts)
                    or PurePosixPath(record["dat_path"]).name != record["dat_name"]
                    or record["url"] != raw_url(commit, record["dat_path"])):
                raise ValueError("DAT record does not match its mapping and pinned source")
            local = Path(entry["local_path"])
            if local.is_symlink() or not local.resolve().is_relative_to(config["cache_dir"]):
                raise ValueError("DAT path leaves cache directory")
            data = local.read_bytes()
            if verify_dat(data, entry["record"]) != entry["sha256"]:
                raise ValueError("cached DAT SHA-256 changed")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CatalogError(f"Cached DAT manifest is unavailable or inconsistent; run sync: {exc}") from exc
