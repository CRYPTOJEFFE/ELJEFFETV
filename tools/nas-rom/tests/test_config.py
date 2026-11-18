"""Configuration boundaries that keep report writes outside the user's ROMs."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from romcatalog.config import ConfigError, REPOSITORY, SNAPSHOT, load_config, mapping_identity


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "config.json"
        self.value = {
            "schema_version": 1,
            "cache_dir": "cache", "state_dir": "state", "report_dir": "reports",
            "mappings": [{"name": "game-boy", "provider": "No-Intro",
                          "rom_dir": "roms/game-boy", "dat_name": "Game Boy.dat"}],
        }

    def load(self, value=None):
        self.path.write_text(json.dumps(self.value if value is None else value), encoding="utf-8")
        return load_config(self.path)

    def test_default_source_is_pinned_and_paths_resolve_from_config(self):
        result = self.load()
        self.assertEqual(result["source"], {"repository": REPOSITORY, "ref": SNAPSHOT})
        self.assertEqual(result["cache_dir"], self.base / "cache")
        self.assertEqual(result["mappings"][0]["rom_dir"], self.base / "roms/game-boy")
        self.assertFalse((self.base / "cache").exists())

    def test_main_is_explicit_opt_in_and_full_sha_is_normalized(self):
        for ref, expected in [("main", "main"), (SNAPSHOT.upper(), SNAPSHOT)]:
            with self.subTest(ref=ref):
                self.value["source"] = {"repository": REPOSITORY, "ref": ref}
                self.assertEqual(self.load()["source"]["ref"], expected)

    def test_arbitrary_refs_and_other_repositories_are_rejected(self):
        for source in [{"ref": "master"}, {"ref": SNAPSHOT[:7]}, {"ref": "../main"},
                       {"ref": 123}, {"repository": "another/repo"}]:
            with self.subTest(source=source):
                self.value["source"] = source
                with self.assertRaises(ConfigError):
                    self.load()

    def test_at_least_one_explicit_enabled_mapping_is_required(self):
        for mappings in [None, [], [{"enabled": False}]]:
            with self.subTest(mappings=mappings):
                self.value["mappings"] = mappings
                with self.assertRaises(ConfigError):
                    self.load()

    def test_selector_is_required_and_must_be_unambiguous(self):
        template = self.value["mappings"][0]
        for fields in [{}, {"dat_name": ""}, {"dat_name": 7},
                       {"dat_name": "a.dat", "dat_path": "root/a.dat"}]:
            with self.subTest(fields=fields):
                self.value["mappings"] = [{k: v for k, v in template.items() if k != "dat_name"} | fields]
                with self.assertRaises(ConfigError):
                    self.load()

    def test_exact_path_selector_is_supported(self):
        mapping = self.value["mappings"][0]
        mapping.pop("dat_name")
        mapping["dat_path"] = "root/normalized/No-Intro/A/Game Boy.dat"
        self.assertEqual(self.load()["mappings"][0]["dat_path"], mapping["dat_path"])

    def test_duplicate_mapping_names_are_case_insensitive(self):
        duplicate = copy.deepcopy(self.value["mappings"][0])
        duplicate["name"] = "GAME-BOY"
        self.value["mappings"].append(duplicate)
        with self.assertRaises(ConfigError):
            self.load()

    def test_mapping_names_cannot_escape_output_file_paths(self):
        for name in ["../roms", "a/b", "a\\b", ".", "", "x" * 65]:
            with self.subTest(name=name):
                self.value["mappings"][0]["name"] = name
                with self.assertRaises(ConfigError):
                    self.load()

    def test_provider_and_enabled_type_are_validated(self):
        for fields in [{"provider": "Pleasuredome"}, {"enabled": "false"}]:
            with self.subTest(fields=fields):
                value = copy.deepcopy(self.value)
                value["mappings"][0].update(fields)
                with self.assertRaises(ConfigError):
                    self.load(value)

    def test_every_writable_directory_is_rejected_inside_or_equal_to_roms(self):
        for key in ["cache_dir", "state_dir", "report_dir"]:
            for destination in ["roms/game-boy", "roms/game-boy/reports"]:
                with self.subTest(key=key, destination=destination):
                    value = copy.deepcopy(self.value)
                    value[key] = destination
                    with self.assertRaises(ConfigError):
                        self.load(value)

    def test_symlinked_output_inside_roms_is_rejected(self):
        roms = self.base / "roms/game-boy"
        roms.mkdir(parents=True)
        (self.base / "output-link").symlink_to(roms, target_is_directory=True)
        self.value["report_dir"] = "output-link/reports"
        with self.assertRaises(ConfigError):
            self.load()

    def test_symlinked_rom_root_is_resolved_before_output_checks(self):
        actual = self.base / "actual-roms"
        actual.mkdir()
        (self.base / "roms-link").symlink_to(actual, target_is_directory=True)
        self.value["mappings"][0]["rom_dir"] = "roms-link"
        self.value["cache_dir"] = "actual-roms/cache"
        with self.assertRaises(ConfigError):
            self.load()

    def test_outputs_are_distinct_and_cannot_be_filesystem_root(self):
        for changes in [{"state_dir": "cache"}, {"report_dir": "/"}]:
            with self.subTest(changes=changes):
                with self.assertRaises(ConfigError):
                    self.load(self.value | changes)

    def test_paths_must_be_nonempty_strings(self):
        for invalid in [None, 12, "", "   ", "bad\x00path"]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigError):
                    self.load(self.value | {"cache_dir": invalid})

    def test_invalid_json_and_missing_config_are_reported(self):
        self.path.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.path)
        with self.assertRaises(ConfigError):
            load_config(self.base / "absent.json")

    def test_mapping_identity_preserves_selector_and_absolute_rom_path(self):
        mapping = self.load()["mappings"][0]
        identity = mapping_identity(mapping)
        self.assertEqual(identity["rom_dir"], str(self.base / "roms/game-boy"))
        self.assertEqual(identity["dat_name"], "Game Boy.dat")


if __name__ == "__main__":
    unittest.main()
