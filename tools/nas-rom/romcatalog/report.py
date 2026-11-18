"""Portable reports; escape filenames and spreadsheet formulas from input data."""
from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path

from .catalog import atomic_write, write_json


def csv_cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if value is None:
        return ""
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row)) or ["status"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: csv_cell(value) for key, value in row.items()})
    atomic_write(path, stream.getvalue().encode("utf-8-sig"))


def _table(rows: list[dict], columns: list[str], limit: int = 250) -> str:
    escape = lambda value: html.escape(str(value), quote=True)
    if not rows:
        return "<p>No entries.</p>"
    header = "".join(f"<th>{escape(col.replace('_', ' '))}</th>" for col in columns)
    body = "".join("<tr>" + "".join(f"<td>{escape(row.get(col, ''))}</td>" for col in columns) + "</tr>"
                   for row in rows[:limit])
    note = f"<p>Showing {min(limit, len(rows)):,} of {len(rows):,}; CSV and JSON contain every entry.</p>"
    return note + f"<div class='scroll'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"


def save_reports(directory: Path, results: list[dict], source: dict) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    summary = {"schema_version": 1, "source": source,
               "scope": "Explicit mapped folders only; counts are DAT entries/sets, not unique games.",
               "complete_scan": all(r["summary"]["complete_scan"] for r in results),
               "mappings": [{"name": r["mapping"]["name"], "rom_root": r.get("rom_root"),
                             "scanned_at": r.get("scanned_at"), "dat": r["dat"], "summary": r["summary"]}
                            for r in results]}
    sections = []
    e = html.escape
    for result in results:
        name = result["mapping"]["name"]
        write_json(directory / f"{name}.json", result)
        issues = ([{"kind": "error", **row} for row in result.get("errors", [])]
                  + [{"kind": "unsupported", **row} for row in result.get("unsupported", [])]
                  + [{"kind": "warning", **row} for row in result.get("warnings", [])]
                  + [{"kind": "unverifiable_dat_entry", **row} for row in result.get("unverifiable", [])])
        rows_by_type = {key: result.get(key, []) for key in
                        ("matched", "missing", "unmatched", "sets", "duplicates")}
        rows_by_type["issues"] = issues
        for kind, rows in rows_by_type.items():
            write_csv(directory / f"{name}-{kind}.csv", rows)
        status = "Complete scan" if result["summary"]["complete_scan"] else "Partial scan — review issues"
        links = " · ".join(f"<a href='{e(name)}-{kind}.csv'>{kind} CSV</a>" for kind in rows_by_type)
        metrics = _table([{ "metric": key.replace("_", " "), "value": value}
                          for key, value in result["summary"].items()], ["metric", "value"])
        sections.append(f"<section><h2>{e(name)}</h2><p class='status'>{status}</p>"
                        f"<p>Folder: <code>{e(str(result.get('rom_root', '')))}</code></p>"
                        f"<p>DAT: {e(str(result['dat'].get('name', '')))}</p><p>{links} · "
                        f"<a href='{e(name)}.json'>Full JSON</a></p>{metrics}"
                        "<details><summary>DAT ROM entries not observed in this folder</summary>"
                        + _table(result.get("missing", []), ["set_name", "rom_name", "size"])
                        + "</details><details><summary>Unreadable, unsupported and unverifiable entries</summary>"
                        + _table(issues, ["kind", "path", "member", "reason", "detail"])
                        + "</details><details><summary>Files without a matching DAT entry</summary>"
                        + _table(result.get("unmatched", []), ["path", "member", "size"])
                        + "</details></section>")
    document = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'">
<title>ELJEFFETV · NAS ROM audit</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e5edf8;font:16px/1.55 system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:36px 24px}h1{font-size:2.2rem;line-height:1.2}h2{margin-top:0}a{color:#7fdac8}
.eyebrow{color:#7fdac8;font-weight:700;letter-spacing:.12em}.intro,section{padding:24px;background:#121e30;border:1px solid #293a50;border-radius:12px;margin:24px 0}
.note{border-left:4px solid #e8b869;padding:12px 18px;background:#27251f}.status{font-weight:bold;color:#e8b869}code{overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:.9rem}td,th{text-align:left;vertical-align:top;padding:9px 12px;border-bottom:1px solid #304059;overflow-wrap:anywhere}
th{text-transform:capitalize;background:#1b2a40}.scroll{overflow:auto}details{margin-top:20px}summary{cursor:pointer;font-weight:650}footer{color:#9cacbf;font-size:.85rem}
</style><main><div class="eyebrow">ELJEFFETV / ROM LIBRARY</div><h1>NAS verification report</h1>"""
    document += f"<div class='intro'><p>Catalog snapshot: <strong>{e(str(source.get('catalog_modified', 'unknown')))}</strong></p>"
    document += f"<p>Commit: <code>{e(str(source.get('commit', '')))}</code></p>"
    document += "<p class='note'>This catalog is a historical reference. An unmatched file is not proof of damage. "
    document += "A missing row means that DAT payload was not observed within its mapped folder. "
    document += "Partial scans, unsupported containers, header differences, patches and newer releases can affect the result.</p>"
    document += "<p>Counts describe DAT entries and sets, which can include variants and clones. "
    document += "Cached hashes reuse unchanged file metadata; use a full rehash for a fresh integrity check.</p>"
    document += "<a href='summary.json'>Summary JSON</a></div>" + "".join(sections)
    document += "<footer>Read-only ROM scan · No ROMs downloaded, renamed or removed · Generated locally</footer></main></html>"
    atomic_write(directory / "index.html", document.encode("utf-8"))
    write_json(directory / "summary.json", summary)
    return summary
