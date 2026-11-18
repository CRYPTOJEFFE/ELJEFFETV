import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from romcatalog.__main__ import main
from romcatalog.config import SNAPSHOT


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roms = self.root / "roms"
        self.roms.mkdir()
        self.payload = b"ELJEFFETV synthetic test payload, not a game"
        digest = hashlib.sha1(self.payload).hexdigest()
        self.dat = (f'<datafile><header><name>Test DAT</name></header><game name="Synthetic">'
                    f'<rom name="original.bin" size="{len(self.payload)}" sha1="{digest}"/>'
                    '</game></datafile>').encode()
        name = "Synthetic.dat"
        self.index = ("Type,Name,URL,CRC,Size\n"
                      f"FILE,{name},https://raw.githubusercontent.com/open-retrogaming-archive/dat-catalog/"
                      f"main/root/normalized/No-Intro/Test/{name},{zlib.crc32(self.dat) & 0xFFFFFFFF},{len(self.dat)}\n").encode()
        self.config = self.root / "config.json"
        self.value = {"schema_version": 1, "cache_dir": "cache", "state_dir": "state", "report_dir": "reports",
                      "source": {"ref": SNAPSHOT}, "mappings": [{"name": "test", "rom_dir": "roms",
                      "provider": "No-Intro", "dat_name": name}]}
        self.config.write_text(json.dumps(self.value), encoding="utf-8")

    def download(self, url, limit):
        if url.endswith("index.csv"):
            return self.index
        if url.endswith("/modified"):
            return b"2023-03-24T20:20:10Z"
        if url.endswith("Synthetic.dat"):
            return self.dat
        self.fail(f"Unexpected network request {url}")

    def invoke(self, command, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main([command, "--config", str(self.config), *extra])

    def test_run_and_offline_audit_with_complete_reports(self):
        original = self.roms / "renamed.bin"
        original.write_bytes(self.payload)
        before = original.stat()
        with patch("romcatalog.catalog.download", self.download):
            self.assertEqual(self.invoke("run"), 0)
        after = original.stat()
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(original.read_bytes(), self.payload)
        summary = json.loads((self.root / "reports/summary.json").read_text())
        self.assertTrue(summary["complete_scan"])
        self.assertEqual(summary["mappings"][0]["summary"]["matched_payloads"], 1)
        self.assertTrue((self.root / "reports/index.html").is_file())
        for kind in ("matched", "missing", "unmatched", "issues", "sets", "duplicates"):
            self.assertTrue((self.root / f"reports/test-{kind}.csv").is_file())
        with patch("romcatalog.catalog.download", side_effect=AssertionError("audit must work offline")):
            self.assertEqual(self.invoke("audit", "--rehash"), 0)

    def test_unmatched_and_missing_are_results_not_runtime_failures(self):
        (self.roms / "different.bin").write_bytes(b"different bytes")
        with patch("romcatalog.catalog.download", self.download):
            self.assertEqual(self.invoke("run"), 0)
        result = json.loads((self.root / "reports/test.json").read_text())
        self.assertEqual(len(result["unmatched"]), 1)
        self.assertEqual(len(result["missing"]), 1)

    def test_unsupported_is_partial(self):
        (self.roms / "unverifiable.chd").write_bytes(b"not a decoded track")
        with patch("romcatalog.catalog.download", self.download):
            self.assertEqual(self.invoke("run"), 3)

    def test_missing_mount_does_not_publish_missing_results(self):
        with patch("romcatalog.catalog.download", self.download):
            self.assertEqual(self.invoke("sync"), 0)
        self.roms.rmdir()
        self.assertEqual(self.invoke("doctor"), 2)
        self.assertEqual(self.invoke("audit"), 2)
        self.assertFalse((self.root / "reports/summary.json").exists())

    def test_doctor_does_not_create_directories(self):
        self.assertEqual(self.invoke("doctor"), 0)
        self.assertFalse((self.root / "cache").exists())
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
