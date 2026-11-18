"""Catalog format, transport bounds, snapshot pinning, and cache integrity."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from romcatalog.catalog import (CatalogError, _check_url, download, list_catalog,
                               load_manifest, parse_index, raw_url, resolve_ref,
                               select_record, sync_catalog, verify_dat, write_json)
from romcatalog.config import REPOSITORY, SNAPSHOT, mapping_identity


DAT = b'<?xml version="1.0"?><datafile><header><name>Synthetic</name></header></datafile>\r\n'
DAT_NAME = "Synthetic, Test (USA).dat"
DAT_PATH = f"root/normalized/No-Intro/Console/{DAT_NAME}"
OTHER_COMMIT = "a" * 40


def csv_bytes(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["Type", "Name", "URL", "CRC", "Size"])
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def dat_row(*, path=DAT_PATH, name=DAT_NAME, owner="open-retrogaming-archive", data=DAT):
    return ["FILE", name,
            f"https://raw.githubusercontent.com/{owner}/dat-catalog/main/{path}",
            str(zlib.crc32(data) & 0xFFFFFFFF), str(len(data))]


class CatalogParsingTests(unittest.TestCase):
    def test_flattened_indexes_skip_directory_and_metadata_file_rows(self):
        data = csv_bytes([["DIRECTORY", "Console", "", "", ""],
                          ["FILE", "modified", "https://legacy.example/modified", "1", "30"],
                          ["FILE", "SBI Subchannels", "https://legacy.example/sbi", "1", "30"],
                          dat_row()])
        records = parse_index(data, "No-Intro", SNAPSHOT)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["dat_name"], DAT_NAME)
        self.assertEqual(records[0]["dat_path"], DAT_PATH)

    def test_legacy_owner_is_rewritten_and_downloads_are_pinned(self):
        for owner in ["open-retrogaming-archive", "videogame-archive"]:
            with self.subTest(owner=owner):
                record = parse_index(csv_bytes([dat_row(owner=owner)]), "No-Intro", SNAPSHOT)[0]
                self.assertEqual(record["url"], raw_url(SNAPSHOT, DAT_PATH))
                self.assertNotIn("/main/", record["url"])
                self.assertEqual(record["source_commit"], SNAPSHOT)
                self.assertIn("%2C", record["url"])

    def test_decimal_crc_is_converted_to_eight_digit_hex(self):
        row = dat_row()
        row[3] = "1688677462"
        record = parse_index(csv_bytes([row]), "No-Intro", SNAPSHOT)[0]
        self.assertEqual(record["dat_crc32"], format(1688677462, "08x"))

    def test_csv_quoting_and_utf8_bom_are_supported(self):
        record = parse_index(b"\xef\xbb\xbf" + csv_bytes([dat_row()]), "No-Intro", SNAPSHOT)[0]
        self.assertEqual(record["dat_name"], DAT_NAME)

    def test_hostile_or_wrong_repository_paths_are_rejected(self):
        good = dat_row()[2]
        urls = [good.replace("https://", "http://"),
                good.replace("raw.githubusercontent.com", "evil.example"),
                good.replace("open-retrogaming-archive", "another-owner"),
                good.replace("/dat-catalog/", "/another-repo/"),
                good.replace("/No-Intro/", "/Redump/"),
                good.replace("/Console/", "/../"),
                good.replace("/Console/", "/%2e%2e/"),
                good.replace("/Console/", "/Console%2f..%2f/"),
                good.replace("/Console/", "/Console%5c../"),
                good.replace("/Console/", "/Console%00/"),
                good.replace("raw.githubusercontent.com", "raw.githubusercontent.com@evil.example"),
                good + "?token=x", good + "#fragment"]
        for url in urls:
            with self.subTest(url=url):
                row = dat_row()
                row[2] = url
                with self.assertRaises(CatalogError):
                    parse_index(csv_bytes([row]), "No-Intro", SNAPSHOT)

    def test_path_filename_mismatch_duplicate_and_invalid_metadata_rejected(self):
        rows = []
        row = dat_row()
        row[1] = "Different.dat"
        rows.append(row)
        for column, value in [(3, "ffffffff"), (3, "-1"), (3, str(2**32)),
                              (4, "0"), (4, "-5"), (4, str(513 * 1024**2))]:
            row = dat_row()
            row[column] = value
            rows.append(row)
        for row in rows:
            with self.subTest(row=row):
                with self.assertRaises(CatalogError):
                    parse_index(csv_bytes([row]), "No-Intro", SNAPSHOT)
        with self.assertRaises(CatalogError):
            parse_index(csv_bytes([dat_row(), dat_row()]), "No-Intro", SNAPSHOT)

    def test_bad_header_unknown_provider_and_empty_catalog_rejected(self):
        for data, provider in [(b"Name,Type\na,FILE\n", "No-Intro"),
                               (csv_bytes([dat_row()]), "Pleasuredome"),
                               (csv_bytes([["DIRECTORY", "Console", "", "", ""]]), "No-Intro"),
                               (csv_bytes([["MYSTERY", "File.dat", "", "", ""]]), "No-Intro")]:
            with self.subTest(provider=provider, data=data):
                with self.assertRaises(CatalogError):
                    parse_index(data, provider, SNAPSHOT)

    def test_same_name_requires_exact_path_when_multiple_catalogs_match(self):
        rows = [dat_row(), dat_row(path=DAT_PATH.replace("/Console/", "/Other/"))]
        records = parse_index(csv_bytes(rows), "No-Intro", SNAPSHOT)
        with self.assertRaises(CatalogError):
            select_record(records, {"name": "console", "dat_name": DAT_NAME})
        selected = select_record(records, {"name": "console", "dat_path": DAT_PATH})
        self.assertEqual(selected["dat_path"], DAT_PATH)
        with self.assertRaises(CatalogError):
            select_record(records, {"name": "console", "dat_name": "Absent.dat"})

    def test_dat_size_and_crc_both_must_match(self):
        record = parse_index(csv_bytes([dat_row()]), "No-Intro", SNAPSHOT)[0]
        self.assertEqual(verify_dat(DAT, record), hashlib.sha256(DAT).hexdigest())
        for data in [DAT + b"x", b"X" + DAT[1:]]:
            with self.subTest(data=data):
                with self.assertRaises(CatalogError):
                    verify_dat(data, record)

    def test_list_catalog_downloads_only_one_flat_provider_index(self):
        with patch("romcatalog.catalog.download", return_value=csv_bytes([dat_row()])) as mocked:
            records = list_catalog("No-Intro", SNAPSHOT)
        self.assertEqual(len(records), 1)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[0], raw_url(SNAPSHOT, "root/normalized/No-Intro/index.csv"))


class TransportTests(unittest.TestCase):
    def test_full_commit_ref_needs_no_network(self):
        with patch("romcatalog.catalog.download") as mocked:
            self.assertEqual(resolve_ref(SNAPSHOT.upper()), SNAPSHOT)
        mocked.assert_not_called()

    def test_main_resolves_to_commit_object_and_bad_responses_reject(self):
        good = {"object": {"type": "commit", "sha": SNAPSHOT}}
        with patch("romcatalog.catalog.download", return_value=json.dumps(good).encode()):
            self.assertEqual(resolve_ref("main"), SNAPSHOT)
        for value in [{}, {"object": {"type": "tag", "sha": SNAPSHOT}},
                      {"object": {"type": "commit", "sha": "short"}}]:
            with self.subTest(value=value):
                with patch("romcatalog.catalog.download", return_value=json.dumps(value).encode()):
                    with self.assertRaises(CatalogError):
                        resolve_ref("main")

    def test_other_refs_and_untrusted_transport_urls_reject(self):
        with self.assertRaises(CatalogError):
            resolve_ref("dev")
        for url in ["http://raw.githubusercontent.com/x", "file:///etc/passwd",
                    "https://evil.example/x", "https://user:pass@api.github.com/x",
                    "https://api.github.com:444/x"]:
            with self.subTest(url=url):
                with self.assertRaises(CatalogError):
                    _check_url(url)

    def test_download_rejects_oversize_response(self):
        class Response(io.BytesIO):
            url = "https://raw.githubusercontent.com/videogame-archive/dat-catalog/main/README.md"
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(b"12345")
        with patch("romcatalog.catalog.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(CatalogError):
                download(Response.url, 4)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.config = {"source": {"repository": REPOSITORY, "ref": SNAPSHOT},
                       "cache_dir": self.base / "cache",
                       "mappings": [{"name": "synthetic", "provider": "No-Intro",
                                     "rom_dir": self.base / "roms", "dat_name": DAT_NAME}]}
        self.downloads = []

    def fake_download(self, url, limit):
        self.downloads.append(url)
        if url.endswith("/index.csv"):
            return csv_bytes([dat_row()])
        if url.endswith("/modified"):
            return b"2023-03-24T20:20:10.073497768Z"
        return DAT

    def sync(self):
        with patch("romcatalog.catalog.download", side_effect=self.fake_download):
            return sync_catalog(self.config)

    def save(self, manifest):
        write_json(self.config["cache_dir"] / "manifest.json", manifest)

    def test_sync_records_pinned_provenance_and_validates_offline(self):
        result = self.sync()
        self.assertEqual(result["source"]["commit"], SNAPSHOT)
        self.assertTrue(all(f"/{SNAPSHOT}/" in url for url in self.downloads))
        with patch("romcatalog.catalog.download") as mocked:
            loaded = load_manifest(self.config)
        mocked.assert_not_called()
        self.assertEqual(loaded["mappings"][0]["mapping"], mapping_identity(self.config["mappings"][0]))

    def test_intact_cached_dat_is_reused_and_corruption_is_redownloaded(self):
        result = self.sync()
        path = Path(result["mappings"][0]["local_path"])
        self.downloads.clear()
        self.sync()
        self.assertFalse(any(url.endswith(".dat") for url in self.downloads))
        path.write_bytes(b"corruption")
        with self.assertRaises(CatalogError):
            load_manifest(self.config)
        self.downloads.clear()
        self.sync()
        self.assertTrue(any(url.endswith(".dat") for url in self.downloads))
        self.assertEqual(path.read_bytes(), DAT)

    def test_failed_sync_does_not_replace_previous_manifest(self):
        result = self.sync()
        manifest_file = self.config["cache_dir"] / "manifest.json"
        previous = manifest_file.read_bytes()
        Path(result["mappings"][0]["local_path"]).unlink()
        def bad_download(url, limit):
            return b"broken" if url.endswith(".dat") else self.fake_download(url, limit)
        with patch("romcatalog.catalog.download", side_effect=bad_download):
            with self.assertRaises(CatalogError):
                sync_catalog(self.config)
        self.assertEqual(manifest_file.read_bytes(), previous)

    def test_main_is_resolved_once_for_entire_sync(self):
        self.config["source"]["ref"] = "main"
        with patch("romcatalog.catalog.resolve_ref", return_value=SNAPSHOT) as ref_mock:
            result = self.sync()
        ref_mock.assert_called_once_with("main")
        self.assertEqual(result["source"]["commit"], SNAPSHOT)
        self.assertTrue(all(f"/{SNAPSHOT}/" in url for url in self.downloads))

    def test_changed_mapping_source_and_dat_digest_are_rejected(self):
        original = self.sync()
        mutations = [lambda m: m["mappings"][0]["mapping"].update(dat_name="Changed.dat"),
                     lambda m: m["source"].update(requested_ref="main"),
                     lambda m: m["source"].update(repository="other/repo"),
                     lambda m: m["mappings"][0].update(sha256="0" * 64),
                     lambda m: m["mappings"][0]["record"].update(size=len(DAT) + 1)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                manifest = copy.deepcopy(original)
                mutate(manifest)
                self.save(manifest)
                with self.assertRaises(CatalogError):
                    load_manifest(self.config)

    def test_cached_dat_cannot_point_outside_cache_or_through_symlink(self):
        original = self.sync()
        outside = self.base / "elsewhere.dat"
        outside.write_bytes(DAT)
        link = self.config["cache_dir"] / "linked.dat"
        link.symlink_to(outside)
        for target in [outside, link]:
            with self.subTest(target=target):
                changed = copy.deepcopy(original)
                changed["mappings"][0]["local_path"] = str(target)
                self.save(changed)
                with self.assertRaises(CatalogError):
                    load_manifest(self.config)

    def test_sync_rejects_nested_cache_symlink_into_rom_folder(self):
        roms = self.config["mappings"][0]["rom_dir"]
        roms.mkdir()
        commit_directory = self.config["cache_dir"] / SNAPSHOT
        commit_directory.mkdir(parents=True)
        (commit_directory / "No-Intro").symlink_to(roms, target_is_directory=True)
        with self.assertRaises(CatalogError):
            self.sync()
        self.assertEqual(list(roms.iterdir()), [])

    def test_pinned_commit_mutation_is_rejected(self):
        manifest = self.sync()
        manifest["source"]["commit"] = OTHER_COMMIT
        self.save(manifest)
        with self.assertRaises(CatalogError):
            load_manifest(self.config)

    def test_dat_record_must_match_mapping_provider_selector_and_commit(self):
        original = self.sync()
        mutations = [{"provider": "Redump"}, {"dat_name": "Other.dat"},
                     {"source_commit": OTHER_COMMIT},
                     {"url": "https://evil.example/Other.dat"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(original)
                changed["mappings"][0]["record"].update(mutation)
                self.save(changed)
                with self.assertRaises(CatalogError):
                    load_manifest(self.config)


if __name__ == "__main__":
    unittest.main()
