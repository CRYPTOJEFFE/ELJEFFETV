"""Synthetic payloads only; no copyrighted game content or upstream DAT copies."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
import warnings
import xml.sax.saxutils
import zipfile
import zlib

from romcatalog.audit import audit_library, parse_dat


def hashes(data):
    return {"crc": f"{zlib.crc32(data) & 0xffffffff:08x}", "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(), "sha256": hashlib.sha256(data).hexdigest()}


def rom(name, data=b"demo", **overrides):
    values = {"name": name, "size": str(len(data)), **hashes(data), **overrides}
    return "<rom " + " ".join(f'{key}={xml.sax.saxutils.quoteattr(str(value))}' for key, value in values.items() if value is not None) + "/>"


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "roms"
        self.root.mkdir()
        self.dat = self.base / "test.dat"

    def tearDown(self):
        self.temp.cleanup()

    def xml(self, body, declaration=""):
        self.dat.write_text(f'<?xml version="1.0"?>{declaration}<datafile><header><name>Synthetic DAT</name></header>{body}</datafile>')

    def game(self, content):
        self.xml(f'<game name="Synthetic Set">{content}</game>')

    def audit(self, **kwargs):
        return audit_library(self.root, self.dat, **kwargs)

    def test_renamed_exact_match_and_no_rom_changes(self):
        content = b"synthetic test payload"
        actual = self.root / "renamed.bin"
        actual.write_bytes(content)
        before = actual.stat()
        self.game(rom("expected.bin", content))
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 1)
        self.assertEqual(result["summary"]["complete_sets"], 1)
        self.assertEqual(result["summary"]["missing_rom_entries"], 0)
        self.assertEqual(result["matched"][0]["matches"][0]["strength"], "sha256")
        self.assertEqual(actual.read_bytes(), content)
        self.assertEqual(actual.stat().st_mtime_ns, before.st_mtime_ns)

    def test_all_declared_hashes_must_match(self):
        (self.root / "a.bin").write_bytes(b"demo")
        self.game(rom("a.bin", crc="00000000"))
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 0)
        self.assertEqual(result["summary"]["unmatched_payloads"], 1)
        self.assertEqual(result["summary"]["missing_rom_entries"], 1)
        self.assertNotIn("corrupt", json.dumps(result).lower())

    def test_size_mismatch_never_matches(self):
        (self.root / "a.bin").write_bytes(b"demo")
        self.game(rom("a.bin", size="5"))
        self.assertEqual(self.audit()["summary"]["matched_payloads"], 0)

    def test_crc_only_is_labeled_weaker(self):
        (self.root / "a.bin").write_bytes(b"demo")
        self.game(rom("a.bin", sha256=None, sha1=None, md5=None))
        self.assertEqual(self.audit()["matched"][0]["matches"][0]["strength"], "crc32_only")

    def test_nodump_invalid_and_disk_entries_are_not_missing(self):
        self.game(rom("a.bin", status="nodump") + rom("b.bin", sha1="bad") + '<disk name="disk" sha1="' + "a" * 40 + '"/>')
        result = self.audit()
        self.assertEqual(result["summary"]["missing_rom_entries"], 0)
        self.assertEqual(len(result["unverifiable"]), 3)
        self.assertEqual(result["sets"][0]["status"], "unverified")

    def test_missing_declared_size_is_unverifiable(self):
        self.game(rom("a.bin", size=None))
        self.assertEqual(self.audit()["unverifiable"][0]["reason"], "size_not_declared")

    def test_multifile_set_is_complete_only_when_all_present(self):
        self.game(rom("track1.bin", b"track 1") + rom("track2.bin", b"track 2"))
        (self.root / "one.bin").write_bytes(b"track 1")
        self.assertEqual(self.audit()["sets"][0]["status"], "incomplete")
        (self.root / "two.bin").write_bytes(b"track 2")
        self.assertEqual(self.audit()["sets"][0]["status"], "complete")

    def test_shared_payload_can_verify_several_catalog_sets(self):
        self.xml('<game name="Parent">' + rom("shared.bin") + '</game><game name="Clone" cloneof="Parent">' + rom("shared.bin") + '</game>')
        (self.root / "file.bin").write_bytes(b"demo")
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 1)
        self.assertEqual(result["summary"]["complete_sets"], 2)
        self.assertEqual(result["sets"][1]["cloneof"], "Parent")

    def test_clrmamepro_with_comments_and_quoted_parentheses(self):
        data = b"demo"
        self.dat.write_text('clrmamepro ( name "Synthetic (test)" )\n// comment\ngame ( name "Set (USA)" description "Quote \\" then \\\\" rom ( name "test.bin" size 4 crc ' + hashes(data)["crc"] + ' sha1 ' + hashes(data)["sha1"] + ' ) )')
        (self.root / "renamed.bin").write_bytes(data)
        result = self.audit()
        self.assertEqual(result["dat"]["format"], "clrmamepro")
        self.assertEqual(result["summary"]["matched_payloads"], 1)

    def test_invalid_cmp_is_rejected(self):
        for text in ('game ( name "x"', 'game ( name "unclosed )', 'game ( name )'):
            self.dat.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_dat(self.dat)

    def test_header_only_and_empty_set_xml_are_rejected(self):
        for body in ("", '<game name="Empty"/>'):
            self.xml(body)
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_dat(self.dat)

    def test_external_doctype_is_allowed_without_fetching(self):
        self.xml('<game name="x">' + rom("a.bin") + '</game>', '<!DOCTYPE datafile SYSTEM "https://example.invalid/no-fetch.dtd">')
        self.assertEqual(parse_dat(self.dat)["format"], "xml")

    def test_internal_entities_and_dtd_rejected(self):
        for declaration in ('<!DOCTYPE datafile [<!ENTITY x "expansion">]>', '<!DOCTYPE datafile [<!ELEMENT datafile ANY>]>'):
            self.xml('<game name="x"/>', declaration)
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                parse_dat(self.dat)

    def test_utf16_xml_supported_and_entities_rejected(self):
        text = '<?xml version="1.0" encoding="utf-16"?><datafile><game name="x">' + rom("a.bin") + '</game></datafile>'
        self.dat.write_bytes(text.encode("utf-16"))
        self.assertEqual(parse_dat(self.dat)["format"], "xml")
        text = text.replace('<datafile>', '<!DOCTYPE datafile [<!ENTITY x "expansion">]><datafile>')
        self.dat.write_bytes(text.encode("utf-16"))
        with self.assertRaises(ValueError):
            parse_dat(self.dat)

    def test_zip_hashes_payload_not_container(self):
        self.game(rom("expected.bin"))
        archive_path = self.root / "renamed.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("subdir/renamed.bin", b"demo")
        before = archive_path.read_bytes()
        result = self.audit()
        self.assertEqual(result["matched"][0]["member"], "subdir/renamed.bin")
        self.assertEqual(result["matched"][0]["hashes"], hashes(b"demo"))
        self.assertEqual(archive_path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [archive_path])

    def test_zip_limits_are_not_false_missing_certainty(self):
        self.game(rom("a.bin"))
        with zipfile.ZipFile(self.root / "a.zip", "w") as archive:
            archive.writestr("a.bin", b"demo")
        result = self.audit(max_member_bytes=2)
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["unsupported"][0]["reason"], "archive_expansion_limit")
        self.assertTrue(result["missing"][0]["provisional"])

    def test_nested_unsupported_container_is_not_a_raw_rom(self):
        self.game(rom("expected.bin"))
        with zipfile.ZipFile(self.root / "nested.zip", "w") as archive:
            archive.writestr("nested.chd", b"demo")
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 0)
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["unsupported"][0]["reason"], "nested_container_requires_format_aware_reader")

    def test_zip_rejects_unsafe_duplicate_and_symlink_members(self):
        self.game(rom("a.bin"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.root / "bad.zip", "w") as archive:
                archive.writestr("../escape.bin", b"demo")
                archive.writestr("C:\\escape.bin", b"demo")
                archive.writestr("same.bin", b"demo")
                archive.writestr("same.bin", b"demo")
                symlink = zipfile.ZipInfo("link.bin")
                symlink.create_system = 3
                symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(symlink, b"somewhere")
        result = self.audit()
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["summary"]["matched_payloads"], 0)
        self.assertEqual(result["summary"]["unsupported"], 5)
        self.assertFalse((self.base / "escape.bin").exists())

    def test_bad_zip_and_bad_member_crc_surface_errors(self):
        self.game(rom("a.bin"))
        (self.root / "broken.zip").write_bytes(b"not a ZIP")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("a.bin", b"demo")
        raw = stream.getvalue().replace(b"demo", b"xxxx", 1)
        (self.root / "badcrc.zip").write_bytes(raw)
        result = self.audit()
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["summary"]["errors"], 2)
        self.assertEqual(result["summary"]["matched_payloads"], 0)

    def test_unsupported_containers_are_reported(self):
        self.game(rom("a.bin"))
        for extension in ("chd", "7z", "rar", "rvz"):
            (self.root / ("a." + extension)).write_bytes(b"demo")
        result = self.audit()
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["summary"]["unsupported"], 4)
        self.assertEqual(result["summary"]["unmatched_payloads"], 0)

    def test_explicitly_catalogued_opaque_container_can_match(self):
        self.game(rom("source.7z"))
        (self.root / "source.7z").write_bytes(b"demo")
        self.assertEqual(self.audit()["summary"]["matched_payloads"], 1)

    def test_opaque_container_with_wrong_hash_is_unsupported(self):
        self.game(rom("source.7z", b"expected") + rom("rom.bin", b"demo"))
        (self.root / "source.7z").write_bytes(b"demo")
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 0)
        self.assertEqual(result["summary"]["unsupported"], 1)
        self.assertFalse(result["summary"]["complete_scan"])

    def test_mixed_zip_catalog_falls_back_to_member_checks(self):
        self.game(rom("whole.zip", b"a different archive") + rom("expected.bin"))
        with zipfile.ZipFile(self.root / "roms.zip", "w") as archive:
            archive.writestr("renamed.bin", b"demo")
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 1)
        self.assertEqual(result["matched"][0]["member"], "renamed.bin")
        self.assertEqual(result["summary"]["missing_rom_entries"], 1)
        self.assertTrue(result["summary"]["complete_scan"])

    def test_exact_whole_zip_match_is_accepted_as_container(self):
        path = self.root / "whole.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("data.bin", b"demo")
        self.game(rom("whole.zip", path.read_bytes()))
        result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 1)
        self.assertIsNone(result["matched"][0]["member"])
        self.assertEqual(result["summary"]["missing_rom_entries"], 0)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinks_are_skipped(self):
        self.game(rom("a.bin"))
        outside = self.base / "outside.bin"
        outside.write_bytes(b"demo")
        (self.root / "link.bin").symlink_to(outside)
        (self.root / "linked-dir").symlink_to(self.base, target_is_directory=True)
        result = self.audit()
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["summary"]["unsupported"], 2)
        self.assertEqual(result["summary"]["matched_payloads"], 0)

    def test_unreadable_directory_is_partial_scan(self):
        self.game(rom("a.bin"))
        def walk(*args, **kwargs):
            kwargs["onerror"](PermissionError(13, "denied", str(self.root / "private")))
            return iter([])
        with mock.patch("romcatalog.audit.os.walk", side_effect=walk):
            result = self.audit()
        self.assertFalse(result["summary"]["complete_scan"])
        self.assertEqual(result["errors"][0]["reason"], "directory_unreadable")

    def test_changing_file_discards_hash_result(self):
        self.game(rom("a.bin"))
        path = self.root / "a.bin"
        path.write_bytes(b"demo")
        from romcatalog import audit
        real_hash = audit._hash_stream
        def change(source, limit):
            result = real_hash(source, limit)
            path.write_bytes(b"different")
            return result
        with mock.patch("romcatalog.audit._hash_stream", side_effect=change):
            result = self.audit()
        self.assertEqual(result["summary"]["matched_payloads"], 0)
        self.assertFalse(result["summary"]["complete_scan"])

    def test_hash_cache_reuses_then_invalidates_and_rehashes(self):
        self.game(rom("a.bin"))
        path = self.root / "a.bin"
        path.write_bytes(b"demo")
        cache = self.base / "state" / "hashes.sqlite3"
        first = self.audit(cache_path=cache)
        self.assertEqual(first["summary"]["payloads_hashed"], 1)
        second = self.audit(cache_path=cache)
        self.assertEqual(second["summary"]["cache_hits"], 1)
        self.assertTrue(second["matched"][0]["cached"])
        self.assertEqual(self.audit(cache_path=cache, rehash=True)["summary"]["payloads_hashed"], 1)
        path.write_bytes(b"xxxx")
        changed = self.audit(cache_path=cache)
        self.assertEqual(changed["summary"]["cache_hits"], 0)
        self.assertEqual(changed["summary"]["matched_payloads"], 0)

    def test_zip_cache_preserves_members(self):
        self.game(rom("a.bin"))
        with zipfile.ZipFile(self.root / "a.zip", "w") as archive:
            archive.writestr("a.bin", b"demo")
        cache = self.base / "cache.sqlite3"
        self.audit(cache_path=cache)
        second = self.audit(cache_path=cache)
        self.assertEqual(second["summary"]["cache_hits"], 1)
        self.assertEqual(second["matched"][0]["member"], "a.bin")

    def test_cache_inside_rom_root_is_rejected(self):
        self.game(rom("a.bin"))
        with self.assertRaises(ValueError):
            self.audit(cache_path=self.root / "cache.sqlite3")
        self.assertFalse((self.root / "cache.sqlite3").exists())

    def test_broken_cache_falls_back_to_hashing(self):
        self.game(rom("a.bin"))
        (self.root / "a.bin").write_bytes(b"demo")
        cache = self.base / "bad-cache.sqlite3"
        cache.write_bytes(b"broken")
        result = self.audit(cache_path=cache)
        self.assertEqual(result["summary"]["payloads_hashed"], 1)
        self.assertEqual(result["summary"]["matched_payloads"], 1)
        self.assertEqual(result["warnings"][0]["reason"], "cache_unavailable")

    def test_duplicates_use_actual_sha256(self):
        self.game(rom("a.bin"))
        (self.root / "one.bin").write_bytes(b"demo")
        (self.root / "two.bin").write_bytes(b"demo")
        (self.root / "different.bin").write_bytes(b"xxxx")
        result = self.audit()
        self.assertEqual(result["summary"]["duplicate_groups"], 1)
        self.assertEqual(result["duplicates"][0]["sha256"], hashes(b"demo")["sha256"])
        self.assertEqual(len(result["duplicates"][0]["locations"]), 2)

    def test_machine_xml_and_empty_payload(self):
        self.xml('<machine name="Machine">' + rom("empty.bin", b"") + '</machine>')
        (self.root / "empty.bin").write_bytes(b"")
        self.assertEqual(self.audit()["summary"]["matched_payloads"], 1)


if __name__ == "__main__":
    unittest.main()
