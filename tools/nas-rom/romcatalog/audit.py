"""Read-only, content-based audits of a declared directory against one DAT.

SPDX-License-Identifier: GPL-2.0-or-later

A missing row means a verifiable DAT ROM entry was not found in this scope. It
does not mean a unique game is missing, or that an unmatched file is corrupt.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
from typing import BinaryIO
import xml.etree.ElementTree as ET
import zipfile
import zlib


HASH_LENGTHS = {"crc": 8, "md5": 32, "sha1": 40, "sha256": 64}
HASH_PRIORITY = ("sha256", "sha1", "md5", "crc")
UNSUPPORTED_CONTAINERS = {
    ".7z", ".rar", ".chd", ".cso", ".zso", ".rvz", ".gcz", ".wia",
    ".wbfs", ".nkit", ".gz", ".bz2", ".xz", ".tar", ".zst",
}
MAX_DAT_BYTES = 256 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
CACHE_VERSION = 1


def _tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _scan_dat_bytes(path: Path) -> tuple[str, bool]:
    """Hash and check the entire input before allowing an XML parser to see it."""
    digest = hashlib.sha256()
    tail = b""
    pending_doctype = b""
    first = b""
    count = 0
    with path.open("rb") as source:
        while block := source.read(CHUNK_BYTES):
            count += len(block)
            if count > MAX_DAT_BYTES:
                raise ValueError("DAT exceeds the 256 MiB parser limit")
            digest.update(block)
            if not first:
                first = block[:4096].replace(b"\x00", b"").lstrip(b"\xef\xbb\xbf \t\r\n")
            # Removing NULs also checks UTF-16/UTF-32 XML declarations.
            checked = tail + block.replace(b"\x00", b"")
            if re.search(br"<!\s*ENTITY\b", checked, re.I):
                raise ValueError("DAT contains an XML entity declaration")
            checked = pending_doctype + checked
            pending_doctype = b""
            for match in re.finditer(br"<!DOCTYPE\b", checked, re.I):
                declaration = checked[match.start():]
                end = declaration.find(b">")
                declaration = declaration if end < 0 else declaration[:end + 1]
                if b"[" in declaration:
                    raise ValueError("DAT contains an internal XML DTD subset")
                if end < 0:
                    if len(declaration) > 16_384:
                        raise ValueError("DAT has an oversized XML declaration")
                    pending_doctype = declaration
            tail = checked[-64:]
    return digest.hexdigest(), first.startswith(b"<") or first.startswith(b"\xff\xfe<") or first.startswith(b"\xfe\xff<")


def _entry(attributes: dict, set_row: dict, entry_id: int, *, disk=False) -> dict:
    reason = None
    try:
        raw_size = attributes.get("size")
        size = int(raw_size) if raw_size is not None else None
        if size is not None and size < 0:
            raise ValueError
    except (ValueError, TypeError):
        size = None
        reason = "invalid_size"
    hashes = {}
    for algorithm, length in HASH_LENGTHS.items():
        raw = attributes.get(algorithm)
        if raw is None and algorithm == "crc":
            raw = attributes.get("crc32")
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
            reason = "invalid_hash"
        else:
            hashes[algorithm] = value
    status = str(attributes.get("status", attributes.get("flags", ""))).lower()
    if disk:
        reason = "disk_hash_requires_format_aware_verification"
    elif "nodump" in status:
        reason = "nodump"
    elif size is None:
        reason = reason or "size_not_declared"
    elif not hashes:
        reason = reason or "no_supported_hash"
    return {
        "entry_id": entry_id, "set_id": set_row["set_id"],
        "set_name": set_row["set_name"], "rom_name": attributes.get("name", ""),
        "size": size, "hashes": hashes, "status": status or None,
        "verifiable": reason is None, "reason": reason,
    }


def _parse_xml(path: Path) -> tuple[dict, list, list]:
    header, entries, sets = {}, [], []
    current = None
    in_header = False
    valid_root = False
    for event, element in ET.iterparse(path, events=("start", "end")):
        name = _tag(element.tag)
        if event == "start":
            if not valid_root:
                if name not in ("datafile", "mame", "softwarelist"):
                    raise ValueError("XML DAT root must be datafile, mame, or softwarelist")
                valid_root = True
            if name in ("game", "machine", "software"):
                if current is not None:
                    raise ValueError("Nested DAT set blocks are unsupported")
                current = {
                    "set_id": len(sets), "set_name": element.attrib.get("name", ""),
                    "cloneof": element.attrib.get("cloneof"), "description": None,
                }
                sets.append(current)
            elif name == "header":
                in_header = True
            continue
        if name in ("rom", "disk") and current is not None:
            entries.append(_entry(dict(element.attrib), current, len(entries), disk=name == "disk"))
        elif name == "description" and current is not None:
            current["description"] = element.text or ""
        elif in_header and name in ("name", "description", "version", "author", "date"):
            header[name] = element.text or ""
        elif name == "header":
            in_header = False
        elif name in ("game", "machine", "software"):
            current = None
        element.clear()
    if not valid_root:
        raise ValueError("Empty XML DAT")
    return header, entries, sets


_CMP_TOKEN = re.compile(r'\s+|//[^\r\n]*|#[^\r\n]*|"(?:\\.|[^"\\])*"|[()]|[^\s()"]+')


def _cmp_tokens(text: str):
    position = 0
    for match in _CMP_TOKEN.finditer(text):
        if match.start() != position:
            raise ValueError("Malformed quoted string in clrmamepro DAT")
        position = match.end()
        value = match.group()
        if value.isspace() or value.startswith(("//", "#")):
            continue
        if value.startswith('"'):
            yield (re.sub(r'\\(["\\])', r'\1', value[1:-1]), False)
        else:
            yield (value, value in ("(", ")"))
    if position != len(text):
        raise ValueError("Malformed trailing content in clrmamepro DAT")


def _parse_cmp(path: Path) -> tuple[dict, list, list]:
    data = path.read_bytes()
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        source = data.decode("cp1252")
    tokens = iter(_cmp_tokens(source))

    def block(depth=0, nested=False):
        if depth > 32:
            raise ValueError("clrmamepro DAT nesting limit exceeded")
        pairs = []
        while (key := next(tokens, None)) is not None:
            if key == (")", True):
                if not nested:
                    raise ValueError("Unexpected closing parenthesis in DAT")
                return pairs
            if key[1]:
                raise ValueError("Expected field name in DAT")
            value = next(tokens, None)
            if value is None or value == (")", True):
                raise ValueError("Missing field value in DAT")
            pairs.append((key[0].lower(), block(depth + 1, True) if value == ("(", True) else value[0]))
        if nested:
            raise ValueError("Unclosed clrmamepro DAT block")
        return pairs

    def scalar_values(pairs):
        return {key: value for key, value in pairs if isinstance(value, str)}

    header, entries, sets = {}, [], []
    for kind, fields in block():
        if not isinstance(fields, list):
            continue
        if kind in ("clrmamepro", "romvault", "header"):
            header.update(scalar_values(fields))
        elif kind in ("game", "machine", "resource"):
            attributes = scalar_values(fields)
            row = {"set_id": len(sets), "set_name": attributes.get("name", ""),
                   "cloneof": attributes.get("cloneof"), "description": attributes.get("description")}
            sets.append(row)
            for child, attributes in fields:
                if child in ("rom", "disk") and isinstance(attributes, list):
                    entries.append(_entry(scalar_values(attributes), row, len(entries), disk=child == "disk"))
    if not sets:
        raise ValueError("No game or machine blocks found in clrmamepro DAT")
    return header, entries, sets


def parse_dat(dat_path: Path) -> dict:
    """Read a Logiqx/XML or clrmamepro DAT without resolving external entities."""
    path = Path(dat_path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("DAT must be a regular file")
    before = _fingerprint(info)
    if before[3] > MAX_DAT_BYTES:
        raise ValueError("DAT exceeds the 256 MiB parser limit")
    sha256, xml = _scan_dat_bytes(path)
    try:
        header, entries, sets = _parse_xml(path) if xml else _parse_cmp(path)
    except ET.ParseError as error:
        raise ValueError(f"Invalid XML DAT: {error}") from error
    if not sets or not entries:
        raise ValueError("DAT must contain at least one set and one ROM or disk entry")
    if _fingerprint(path.stat()) != before:
        raise ValueError("DAT changed while being read; retry with a stable copy")
    return {"path": str(path.resolve()), "name": header.get("name", path.name),
            "format": "xml" if xml else "clrmamepro", "sha256": sha256,
            "header": header, "entries": entries, "sets": sets}


def _hash_stream(source: BinaryIO, limit: int) -> dict:
    hashes = {name: hashlib.new(name, usedforsecurity=False) for name in ("md5", "sha1", "sha256")}
    crc, size = 0, 0
    while chunk := source.read(min(CHUNK_BYTES, limit - size + 1)):
        size += len(chunk)
        if size > limit:
            raise ValueError("Payload exceeded its declared size or configured limit")
        crc = zlib.crc32(chunk, crc)
        for digest in hashes.values():
            digest.update(chunk)
    return {"size": size, "hashes": {"crc": f"{crc & 0xffffffff:08x}", **{name: value.hexdigest() for name, value in hashes.items()}}}


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    return bool(normalized) and not (
        "\x00" in normalized or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in PurePosixPath(normalized).parts
    )


class _Cache:
    def __init__(self, path: Path | None, warnings: list):
        self.database = None
        self.warnings = warnings
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.database = sqlite3.connect(path, timeout=5)
            self.database.execute("CREATE TABLE IF NOT EXISTS hash_cache (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payloads TEXT NOT NULL)")
            self.database.commit()
        except (OSError, sqlite3.Error) as error:
            self._failure(error)

    def _failure(self, error):
        if not any(row.get("reason") == "cache_unavailable" for row in self.warnings):
            self.warnings.append({"reason": "cache_unavailable", "detail": str(error), "action": "Hashing file contents directly"})
        if self.database is not None:
            self.database.close()
            self.database = None

    def get(self, path: Path, fingerprint: tuple):
        if self.database is None:
            return None
        try:
            row = self.database.execute("SELECT payloads FROM hash_cache WHERE path=? AND fingerprint=?", (str(path), json.dumps((CACHE_VERSION, *fingerprint)))).fetchone()
            if row is None:
                return None
            result = json.loads(row[0])
            if not isinstance(result, list) or not result:
                return None
            for payload in result:
                if not isinstance(payload, dict) or "member" not in payload or type(payload.get("size")) is not int or payload["size"] < 0:
                    return None
                member = payload.get("member")
                if member is not None and (not isinstance(member, str) or not _safe_member(member)):
                    return None
                hashes = payload.get("hashes", {})
                if not isinstance(hashes, dict) or any(not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", hashes.get(name, "")) for name, length in HASH_LENGTHS.items()):
                    return None
            return [{key: payload[key] for key in ("member", "size", "hashes")} for payload in result]
        except (sqlite3.Error, TypeError, ValueError) as error:
            self._failure(error)
            return None

    def put(self, path: Path, fingerprint: tuple, payloads: list):
        if self.database is None or not payloads:
            return
        try:
            self.database.execute("INSERT OR REPLACE INTO hash_cache VALUES (?, ?, ?)", (str(path), json.dumps((CACHE_VERSION, *fingerprint)), json.dumps(payloads)))
            self.database.commit()
        except sqlite3.Error as error:
            self._failure(error)

    def close(self):
        if self.database is not None:
            self.database.close()


def _read_file(path: Path, before: os.stat_result, *, zipped: bool,
               max_member_bytes: int, max_archive_bytes: int) -> tuple[list, list, list]:
    payloads, unsupported, errors = [], [], []
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as source:
        if _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
            raise OSError("File changed or became a symlink before opening")
        if zipped:
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                counts = Counter(info.filename for info in members if not info.is_dir())
                expanded = 0
                for info in members:
                    if info.is_dir():
                        continue
                    row = {"member": info.filename}
                    mode = (info.external_attr >> 16) & 0xffff
                    if not _safe_member(info.filename):
                        unsupported.append({**row, "reason": "unsafe_archive_member_path"})
                    elif counts[info.filename] > 1:
                        unsupported.append({**row, "reason": "duplicate_archive_member_name"})
                    elif stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                        unsupported.append({**row, "reason": "non_regular_archive_member"})
                    elif info.flag_bits & 1:
                        unsupported.append({**row, "reason": "encrypted_archive_member"})
                    elif info.file_size > max_member_bytes or expanded + info.file_size > max_archive_bytes:
                        unsupported.append({**row, "reason": "archive_expansion_limit", "size": info.file_size})
                    else:
                        expanded += info.file_size
                        try:
                            with archive.open(info) as member:
                                payload = _hash_stream(member, info.file_size)
                            if payload["size"] != info.file_size:
                                raise ValueError("ZIP member size does not match its directory entry")
                            payloads.append({**row, **payload})
                        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError, zlib.error) as error:
                            errors.append({**row, "reason": "archive_member_read_failed", "detail": str(error)})
        else:
            payload = _hash_stream(source, before.st_size)
            if payload["size"] != before.st_size:
                raise OSError("File size changed during hashing")
            payloads.append({"member": None, **payload})
        if _fingerprint(os.fstat(source.fileno())) != _fingerprint(before):
            raise OSError("File changed during hashing")
    if _fingerprint(path.lstat()) != _fingerprint(before):
        raise OSError("File changed during hashing")
    return payloads, unsupported, errors


def audit_library(rom_root: Path, dat_path: Path, *, cache_path: Path | None = None,
                  rehash: bool = False, max_member_bytes: int = 32 * 1024**3,
                  max_archive_bytes: int = 128 * 1024**3) -> dict:
    """Audit one explicit directory. ROM bytes are never written or extracted.

    Cached hashes require unchanged device/inode/mode/size/mtime/ctime. Use
    ``rehash=True`` for a fresh integrity check after copying or restoring files.
    """
    original_root = Path(rom_root).absolute()
    if original_root.is_symlink() or not original_root.is_dir():
        raise ValueError("ROM root must be an existing, non-symlink directory")
    root = original_root.resolve()
    if max_member_bytes <= 0 or max_archive_bytes <= 0:
        raise ValueError("Archive expansion limits must be positive")
    if cache_path is not None:
        cache_path = Path(cache_path).resolve()
        if cache_path == root or root in cache_path.parents:
            raise ValueError("The hash cache must be outside the ROM directory")
    parsed = parse_dat(dat_path)
    entries = parsed["entries"]
    index = defaultdict(list)
    for entry in entries:
        if entry["verifiable"]:
            algorithm = next(name for name in HASH_PRIORITY if name in entry["hashes"])
            index[(entry["size"], algorithm, entry["hashes"][algorithm])].append(entry)
    opaque_formats = {Path(entry["rom_name"]).suffix.lower() for entry in entries if entry["verifiable"]}
    result = {
        "schema_version": 1, "scanned_at": datetime.now(timezone.utc).isoformat(),
        "rom_root": str(root), "dat": {key: parsed[key] for key in ("path", "name", "format", "sha256", "header")},
        "matched": [], "unmatched": [], "unsupported": [], "errors": [],
        "warnings": [], "duplicates": [], "missing": [], "sets": [],
        "unverifiable": [entry for entry in entries if not entry["verifiable"]],
    }
    cache = _Cache(cache_path, result["warnings"])
    matched_ids = set()
    duplicate_index = defaultdict(list)
    files_seen = payloads_hashed = cache_hits = 0

    def walk_error(error):
        result["errors"].append({"path": str(getattr(error, "filename", root)), "reason": "directory_unreadable", "detail": str(error)})

    def find_matches(payload, container_suffix=None):
        matches = []
        for algorithm in HASH_PRIORITY:
            for entry in index.get((payload["size"], algorithm, payload["hashes"][algorithm]), []):
                if container_suffix is not None and Path(entry["rom_name"]).suffix.lower() != container_suffix:
                    continue
                if all(payload["hashes"].get(name) == value for name, value in entry["hashes"].items()):
                    matches.append({**entry, "strength": "crc32_only" if algorithm == "crc" else algorithm})
        return matches

    def accept(payload, relative, cached, matches=None):
        row = {"path": relative, **payload, "cached": cached}
        matches = find_matches(payload) if matches is None else matches
        if matches:
            matched_ids.update(entry["entry_id"] for entry in matches)
            result["matched"].append({**row, "matches": matches})
        else:
            result["unmatched"].append({**row, "reason": "no_exact_size_and_hash_match_in_selected_dat"})
        duplicate_index[(row["size"], row["hashes"]["sha256"])].append({"path": relative, "member": row["member"]})

    def load_payloads(path, before, relative, zipped):
        nonlocal payloads_hashed, cache_hits
        mode = "zip" if zipped else "raw"
        fingerprint = (*_fingerprint(before), mode, max_member_bytes, max_archive_bytes)
        cache_key = str(path) + "\x00" + mode
        payloads = None if rehash else cache.get(cache_key, fingerprint)
        if payloads is not None:
            if _fingerprint(path.lstat()) != _fingerprint(before):
                raise OSError("File changed while reading its cache record")
            cache_hits += 1
            return payloads, True
        payloads, skipped, errors = _read_file(path, before, zipped=zipped, max_member_bytes=max_member_bytes, max_archive_bytes=max_archive_bytes)
        result["unsupported"].extend({"path": relative, **row} for row in skipped)
        result["errors"].extend({"path": relative, **row} for row in errors)
        payloads_hashed += len(payloads)
        if not skipped and not errors:
            cache.put(cache_key, fingerprint, payloads)
        return payloads, False

    try:
        for directory, folders, files in os.walk(root, followlinks=False, onerror=walk_error):
            folders.sort()
            files.sort()
            for name in folders[:]:
                path = Path(directory, name)
                try:
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        folders.remove(name)
                        result["unsupported"].append({"path": path.relative_to(root).as_posix(), "member": None, "reason": "symlink_or_non_directory_skipped"})
                except OSError as error:
                    folders.remove(name)
                    walk_error(error)
            for name in files:
                files_seen += 1
                path = Path(directory, name)
                relative = path.relative_to(root).as_posix()
                try:
                    before = path.lstat()
                    if not stat.S_ISREG(before.st_mode):
                        result["unsupported"].append({"path": relative, "member": None, "reason": "symlink_or_non_regular_file_skipped"})
                        continue
                    suffix = path.suffix.lower()
                    if suffix in UNSUPPORTED_CONTAINERS and suffix not in opaque_formats:
                        result["unsupported"].append({"path": relative, "member": None, "reason": "container_requires_format_aware_reader", "format": suffix})
                        continue
                    # Some source-code DATs describe whole .zip/.7z files. Only
                    # exact matches to those explicit container entries count;
                    # their presence never changes how unrelated ROM ZIPs scan.
                    if suffix in opaque_formats and suffix in UNSUPPORTED_CONTAINERS | {".zip"}:
                        raw_payloads, raw_cached = load_payloads(path, before, relative, False)
                        matches = find_matches(raw_payloads[0], container_suffix=suffix)
                        if matches:
                            accept(raw_payloads[0], relative, raw_cached, matches)
                            continue
                        if suffix != ".zip":
                            result["unsupported"].append({"path": relative, "member": None, "reason": "container_requires_format_aware_reader", "format": suffix, "detail": "Container bytes do not match a DAT entry explicitly describing this container format"})
                            continue
                    payloads, cached = load_payloads(path, before, relative, suffix == ".zip")
                    for payload in payloads:
                        member_suffix = Path(payload["member"]).suffix.lower() if payload["member"] is not None else ""
                        if member_suffix in UNSUPPORTED_CONTAINERS | {".zip"}:
                            matches = find_matches(payload, container_suffix=member_suffix)
                            if matches:
                                accept(payload, relative, cached, matches)
                            else:
                                result["unsupported"].append({"path": relative, "member": payload["member"], "reason": "nested_container_requires_format_aware_reader", "format": member_suffix})
                        else:
                            accept(payload, relative, cached)
                except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError, zlib.error) as error:
                    result["errors"].append({"path": relative, "member": None, "reason": "file_unreadable_or_changed", "detail": str(error)})
    finally:
        cache.close()

    result["missing"] = [entry for entry in entries if entry["verifiable"] and entry["entry_id"] not in matched_ids]
    for (size, digest), locations in duplicate_index.items():
        if len(locations) > 1:
            result["duplicates"].append({"size": size, "sha256": digest, "locations": locations})
    entries_by_set = defaultdict(list)
    for entry in entries:
        entries_by_set[entry["set_id"]].append(entry)
    for dat_set in parsed["sets"]:
        members = entries_by_set[dat_set["set_id"]]
        verifiable = [entry for entry in members if entry["verifiable"]]
        found = sum(entry["entry_id"] in matched_ids for entry in verifiable)
        unverifiable = len(members) - len(verifiable)
        status = "complete" if verifiable and found == len(verifiable) and unverifiable == 0 else "incomplete" if found else "unverified"
        result["sets"].append({**dat_set, "status": status, "rom_entries": len(members), "verifiable_entries": len(verifiable), "matched_entries": found, "missing_entries": len(verifiable) - found, "unverifiable_entries": unverifiable})
    result["dat"].update({"rom_entries": len(entries), "set_entries": len(parsed["sets"]), "unverifiable_entries": len(result["unverifiable"])})
    result["summary"] = {
        "complete_scan": not result["errors"] and not result["unsupported"],
        "files_seen": files_seen, "payloads_hashed": payloads_hashed, "cache_hits": cache_hits,
        "matched_payloads": len(result["matched"]), "unmatched_payloads": len(result["unmatched"]),
        "missing_rom_entries": len(result["missing"]),
        "complete_sets": sum(row["status"] == "complete" for row in result["sets"]),
        "incomplete_sets": sum(row["status"] == "incomplete" for row in result["sets"]),
        "unverified_sets": sum(row["status"] == "unverified" for row in result["sets"]),
        "errors": len(result["errors"]), "unsupported": len(result["unsupported"]),
        "duplicate_groups": len(result["duplicates"]),
    }
    result["missing"] = [{**entry, "provisional": not result["summary"]["complete_scan"]} for entry in result["missing"]]
    return result
