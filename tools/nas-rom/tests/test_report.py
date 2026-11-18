"""Report safety and completeness tests using synthetic collection records."""
from __future__ import annotations

import csv
import html
import json
import tempfile
import unittest
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path

from romcatalog.report import csv_cell, save_reports, write_csv


class TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


def synthetic_result(name="synthetic", complete=True):
    return {
        "mapping": {"name": name},
        "rom_root": "/synthetic/read-only-roms",
        "scanned_at": "2026-01-01T00:00:00Z",
        "dat": {"name": "Synthetic DAT", "source_format": "xml"},
        "summary": {"complete_scan": complete, "verified_rom_entries": 1},
        "matched": [{"path": "renamed.bin", "rom_name": "reference.bin", "size": 4}],
        "missing": [{"set_name": "Reference set", "rom_name": "missing.bin", "size": 8}],
        "unmatched": [{"path": "unknown.bin", "size": 12}],
        "sets": [{"set_name": "Reference set", "status": "incomplete"}],
        "duplicates": [{"sha256": "a" * 64, "paths": ["one.bin", "two.bin"]}],
        "errors": [],
        "unsupported": [],
        "unverifiable": [],
    }


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


class ReportTests(unittest.TestCase):
    def test_csv_formula_prefixes_are_neutralized_without_losing_text(self):
        formula_inputs = [
            "=1+1", "+SUM(1,2)", "-2+3", "@SUM(1,2)",
            " \t=HYPERLINK(\"https://example.invalid\",\"x\")",
            "\r+1", "\n@SUM(1,2)", "\t-1", '=1+1"\r\n=2+2',
        ]
        safe_inputs = ["ordinary.bin", "日本語.bin", "contains,comma.bin",
                       'a"quote.bin', "a\nline.bin", "", "0"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.csv"
            rows = [{"path": value, "size": 1} for value in formula_inputs + safe_inputs]
            write_csv(path, rows)
            loaded = read_csv(path)
            self.assertEqual(len(loaded), len(rows))
            for row, original in zip(loaded, formula_inputs + safe_inputs):
                expected = "'" + original if original in formula_inputs else original
                self.assertEqual(row["path"], expected)
                self.assertEqual(row["size"], "1")
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_csv_preserves_fields_and_serializes_nested_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.csv"
            rows = [{"path": "one.bin", "extra": None},
                    {"path": "two.bin", "members": ["a,b", 'c"d', "=not-a-cell"],
                     "details": {"name": "日本語", "size": 10}}]
            write_csv(path, rows)
            loaded = read_csv(path)
            self.assertEqual(set(loaded[0]), {"path", "extra", "members", "details"})
            self.assertEqual(loaded[0]["extra"], "")
            self.assertEqual(json.loads(loaded[1]["members"]), rows[1]["members"])
            self.assertEqual(json.loads(loaded[1]["details"]), rows[1]["details"])
            self.assertEqual(csv_cell(None), "")

    def test_html_escapes_untrusted_paths_dat_metadata_and_issue_text(self):
        attack = '\"><img src=x onerror="alert(1)"><script>alert(2)</script>&'
        result = synthetic_result()
        result["rom_root"] = attack
        result["dat"]["name"] = attack
        result["missing"][0].update(set_name=attack, rom_name=attack)
        result["unmatched"][0]["path"] = attack
        result["errors"] = [{"path": attack, "message": attack}]
        result["unsupported"] = [{"path": attack, "reason": attack}]
        result["unverifiable"] = [{"rom_name": attack, "reason": attack}]
        source = {"catalog_modified": attack, "commit": attack}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_reports(directory, [result], source)
            document = (directory / "index.html").read_text()
            self.assertNotIn(attack, document)
            self.assertIn(html.escape(attack), document)
            parsed = TagCollector()
            parsed.feed(document)
            self.assertFalse(set(parsed.tags) & {"script", "img", "iframe", "svg", "object", "embed"})
            self.assertFalse(any(key.lower().startswith("on") for key, _ in parsed.attributes))
            self.assertIn("Content-Security-Policy", document)
            self.assertEqual(json.loads((directory / "synthetic.json").read_text()), result)

    def test_preview_limit_does_not_drop_any_json_or_csv_rows(self):
        result = synthetic_result()
        result["missing"] = [
            {"set_name": f"Set {i:04}", "rom_name": f"row-marker-{i:04}.bin",
             "size": i, "additional_evidence": {"index": i, "expected": ["crc", "sha1"]}}
            for i in range(301)
        ]
        before = deepcopy(result)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_reports(directory, [result], {"commit": "1" * 40})
            document = (directory / "index.html").read_text()
            self.assertIn("Showing 250 of 301", document)
            self.assertIn("row-marker-0249.bin", document)
            self.assertNotIn("row-marker-0250.bin", document)
            loaded_json = json.loads((directory / "synthetic.json").read_text())
            self.assertEqual(loaded_json["missing"], before["missing"])
            loaded_csv = read_csv(directory / "synthetic-missing.csv")
            self.assertEqual(len(loaded_csv), 301)
            self.assertEqual(loaded_csv[-1]["rom_name"], "row-marker-0300.bin")
            self.assertEqual(json.loads(loaded_csv[-1]["additional_evidence"]),
                             before["missing"][-1]["additional_evidence"])
            self.assertEqual(result, before)

    def test_all_export_categories_and_formula_source_values_are_preserved(self):
        result = synthetic_result()
        result["matched"][0]["path"] = "=malicious-filename.bin"
        result["errors"] = [{"path": "bad.zip", "message": "Synthetic error"}]
        result["unsupported"] = [{"path": "disc.chd", "reason": "Synthetic unsupported container"}]
        result["unverifiable"] = [{"rom_name": "undumped.bin", "reason": "nodump"}]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            save_reports(directory, [result], {"commit": "1" * 40})
            required = {"index.html", "summary.json", "synthetic.json"}
            required.update(f"synthetic-{kind}.csv" for kind in
                            ["matched", "missing", "unmatched", "issues", "sets", "duplicates"])
            self.assertEqual({p.name for p in directory.iterdir()}, required)
            matched_csv = read_csv(directory / "synthetic-matched.csv")
            self.assertEqual(matched_csv[0]["path"], "'=malicious-filename.bin")
            loaded_json = json.loads((directory / "synthetic.json").read_text())
            self.assertEqual(loaded_json["matched"][0]["path"], "=malicious-filename.bin")
            issues = read_csv(directory / "synthetic-issues.csv")
            self.assertEqual(len(issues), 3)
            self.assertEqual({r["kind"] for r in issues},
                             {"error", "unsupported", "unverifiable_dat_entry"})

    def test_summary_reports_partial_when_one_mapping_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = {"commit": "1" * 40, "catalog_modified": "2023-03-24"}
            summary = save_reports(directory, [synthetic_result("complete"),
                                               synthetic_result("partial", complete=False)], source)
            self.assertIs(summary["complete_scan"], False)
            self.assertEqual(summary["source"], source)
            self.assertEqual([r["name"] for r in summary["mappings"]], ["complete", "partial"])
            self.assertEqual(json.loads((directory / "summary.json").read_text()), summary)
            self.assertIn("Partial scan", (directory / "index.html").read_text())

    def test_empty_category_is_an_usable_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "empty.csv"
            write_csv(path, [])
            self.assertEqual(read_csv(path), [])
            self.assertEqual(path.read_text(encoding="utf-8-sig").strip(), "status")


if __name__ == "__main__":
    unittest.main()
