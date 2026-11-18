"""Explicit library mappings; never infer a NAS volume or console folder."""
from __future__ import annotations

import json
import re
from pathlib import Path

REPOSITORY = "videogame-archive/dat-catalog"
SNAPSHOT = "843a568f31ccd7847a3cea1f710d351119a52960"
PROVIDERS = ("No-Intro", "Redump", "TOSEC", "TOSEC-ISO", "TOSEC-PIX")


class ConfigError(ValueError):
    pass


def _path(value, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{label} must be a nonempty path")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(filename: str | Path) -> dict:
    path = Path(filename).expanduser().resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ConfigError("Config must have schema_version: 1")
    source = value.get("source", {})
    if not isinstance(source, dict) or source.get("repository", REPOSITORY) != REPOSITORY:
        raise ConfigError(f"source.repository must be {REPOSITORY}")
    ref = source.get("ref", SNAPSHOT)
    if not isinstance(ref, str) or not (ref == "main" or re.fullmatch(r"[a-fA-F0-9]{40}", ref)):
        raise ConfigError("source.ref must be main or a full 40-character commit SHA")
    result = {"schema_version": 1, "config_path": path,
              "source": {"repository": REPOSITORY, "ref": ref.lower()}, "mappings": []}
    for key in ("cache_dir", "state_dir", "report_dir"):
        result[key] = _path(value.get(key), path.parent, key)
        if result[key] == Path(result[key].anchor):
            raise ConfigError(f"{key} cannot be a filesystem root")
    directories = [result[k] for k in ("cache_dir", "state_dir", "report_dir")]
    if len(set(directories)) != len(directories):
        raise ConfigError("cache_dir, state_dir and report_dir must be distinct directories")
    mappings = value.get("mappings")
    if not isinstance(mappings, list):
        raise ConfigError("mappings must be a list")
    names = set()
    for item in mappings:
        if not isinstance(item, dict):
            raise ConfigError("Each mapping must be an object")
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ConfigError("enabled must be true or false")
        if item.get("enabled", True) is False:
            continue
        name = item.get("name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ConfigError("Mapping name must contain 1-64 letters, digits, hyphens or underscores")
        if name.casefold() in names:
            raise ConfigError(f"Duplicate mapping name: {name}")
        if name.casefold() == "summary":
            raise ConfigError("Mapping name 'summary' is reserved for the combined report")
        names.add(name.casefold())
        if item.get("provider") not in PROVIDERS:
            raise ConfigError(f"{name}: provider must be one of {', '.join(PROVIDERS)}")
        selectors = [key for key in ("dat_name", "dat_path") if item.get(key)]
        if len(selectors) != 1 or not isinstance(item[selectors[0]], str):
            raise ConfigError(f"{name}: supply exactly one of dat_name or dat_path from the catalog command")
        rom_dir = _path(item.get("rom_dir"), path.parent, f"{name}.rom_dir")
        for directory in directories:
            if directory == rom_dir or directory.is_relative_to(rom_dir):
                raise ConfigError(f"Output/state directory {directory} must be outside ROM folder {rom_dir}")
        result["mappings"].append({"name": name, "rom_dir": rom_dir,
                                   "provider": item["provider"], selectors[0]: item[selectors[0]]})
    if not result["mappings"]:
        raise ConfigError("Enable at least one explicit ROM-to-DAT mapping")
    return result


def mapping_identity(mapping: dict) -> dict:
    return {key: str(value) for key, value in mapping.items()}
