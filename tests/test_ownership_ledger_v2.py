"""OpenAI Codex regression tests for canonical ownership-ledger schema v2."""

from __future__ import annotations

import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from execution import ownership_guard as guard
from execution.ownership_ledger_codec import (
    LedgerSchemaError,
    convert,
    dumps,
    loads_strict,
    ownership_fingerprint,
    validate,
)
from scripts import migrate_ownership_ledger_v2 as migration


def ledger_v1() -> dict:
    tiers = {}
    for tier, qty in (
        ("daytrade", 1.0),
        ("intraday", 2.0),
        ("qhm", 3.0),
        ("forever6", 4.0),
    ):
        tiers[tier] = {
            "qty": qty,
            "avg_cost": 100.0 + qty,
            "last_fill_id": f"fill-{tier}",
            "extension": {"preserved": True},
        }
    return {
        "version": 1,
        "last_reconciled_utc": "2026-10-09T00:00:00+00:00",
        "positions": {
            "NVDA": {
                "alpaca_net_qty": 10.0,
                "tiers": tiers,
                "drift": 0.0,
                "position_extension": "keep",
            }
        },
        "root_extension": [1, 2, 3],
    }


class CodecTests(unittest.TestCase):
    def test_round_trip_preserves_each_owner_and_extensions(self):
        source = ledger_v1()
        canonical = convert(source, 2)
        self.assertEqual(canonical["version"], 2)
        self.assertEqual(
            set(canonical["positions"]["NVDA"]["tiers"]),
            {"day", "swing", "qhm", "forever_6"},
        )
        self.assertEqual(
            ownership_fingerprint(source), ownership_fingerprint(canonical)
        )
        self.assertEqual(convert(canonical, 1), source)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(LedgerSchemaError):
            loads_strict('{"version":1,"version":2}')
        with self.assertRaises(LedgerSchemaError):
            loads_strict(
                '{"version":1,"last_reconciled_utc":null,"positions":{"X":{"a":1,"a":2}}}'
            )

    def test_schema_matrix_rejects_mixed_missing_and_bad_numbers(self):
        cases = []
        mixed = ledger_v1()
        mixed["positions"]["NVDA"]["tiers"]["day"] = mixed["positions"]["NVDA"][
            "tiers"
        ].pop("daytrade")
        cases.append(mixed)
        missing = ledger_v1()
        missing["positions"]["NVDA"]["tiers"].pop("qhm")
        cases.append(missing)
        bad_bool = ledger_v1()
        bad_bool["positions"]["NVDA"]["tiers"]["qhm"]["qty"] = True
        cases.append(bad_bool)
        bad_inf = ledger_v1()
        bad_inf["positions"]["NVDA"]["drift"] = math.inf
        cases.append(bad_inf)
        bad_symbol = ledger_v1()
        bad_symbol["positions"]["nvda"] = bad_symbol["positions"].pop("NVDA")
        cases.append(bad_symbol)
        bad_fill = ledger_v1()
        bad_fill["positions"]["NVDA"]["tiers"]["qhm"]["last_fill_id"] = 7
        cases.append(bad_fill)
        for candidate in cases:
            with self.subTest(candidate=candidate):
                with self.assertRaises(LedgerSchemaError):
                    validate(candidate)

    def test_numeric_strings_are_rejected_for_every_known_numeric_field(self):
        mutators = (
            lambda row: row["positions"]["NVDA"].__setitem__("alpaca_net_qty", "10"),
            lambda row: row["positions"]["NVDA"].__setitem__("drift", "0"),
            lambda row: row["positions"]["NVDA"]["tiers"]["qhm"].__setitem__(
                "qty", "3"
            ),
            lambda row: row["positions"]["NVDA"]["tiers"]["qhm"].__setitem__(
                "avg_cost", "103"
            ),
        )
        for mutate in mutators:
            candidate = ledger_v1()
            mutate(candidate)
            with self.assertRaises(LedgerSchemaError):
                validate(candidate)

    def test_conversion_does_not_mutate_input(self):
        source = ledger_v1()
        before = json.dumps(source, sort_keys=True)
        convert(source, 2)
        self.assertEqual(json.dumps(source, sort_keys=True), before)


class GuardBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(guard, "_LEDGER_PATH", root / "ownership_ledger.json"),
            mock.patch.object(
                guard, "_LEDGER_BAK_PATH", root / "ownership_ledger.bak.json"
            ),
            mock.patch.object(
                guard, "_LEDGER_MIGRATION_LOCK_PATH", root / ".migration.lock"
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_routine_save_preserves_current_schema(self):
        guard._LEDGER_PATH.write_bytes(dumps(ledger_v1(), target=1))
        guard.save_ledger(guard.load_ledger())
        self.assertEqual(loads_strict(guard._LEDGER_PATH.read_bytes())["version"], 1)
        guard._LEDGER_PATH.write_bytes(dumps(ledger_v1(), target=2))
        guard.save_ledger(guard.load_ledger())
        self.assertEqual(loads_strict(guard._LEDGER_PATH.read_bytes())["version"], 2)

    def test_v2_load_returns_fresh_v1_compatibility_and_canonical_view(self):
        guard._LEDGER_PATH.write_bytes(dumps(ledger_v1(), target=2))
        compat = guard.load_ledger()
        canonical = guard.load_canonical_ledger()
        self.assertEqual(compat["version"], 1)
        self.assertIn("forever6", compat["positions"]["NVDA"]["tiers"])
        self.assertEqual(canonical["version"], 2)
        self.assertIn("forever_6", canonical["positions"]["NVDA"]["tiers"])
        self.assertEqual(guard.protected_floor(compat, "NVDA"), 7.0)

    def test_v2_backup_preserves_forever6_fallback_floor(self):
        guard._LEDGER_BAK_PATH.write_bytes(dumps(ledger_v1(), target=2))
        backup = guard._load_bak_ledger()
        self.assertEqual(guard.tier_qty(backup, "NVDA", "forever6"), 4.0)
        self.assertEqual(guard.protected_floor(backup, "NVDA"), 7.0)

    def test_invalid_candidate_leaves_current_and_backup_unchanged(self):
        original = dumps(ledger_v1(), target=1)
        backup = b"sentinel-backup"
        guard._LEDGER_PATH.write_bytes(original)
        guard._LEDGER_BAK_PATH.write_bytes(backup)
        invalid = ledger_v1()
        invalid["positions"]["NVDA"]["tiers"]["qhm"]["qty"] = True
        with self.assertRaises(LedgerSchemaError):
            guard.save_ledger(invalid)
        self.assertEqual(guard._LEDGER_PATH.read_bytes(), original)
        self.assertEqual(guard._LEDGER_BAK_PATH.read_bytes(), backup)

    def test_corrupt_current_cannot_be_laundered_by_valid_candidate(self):
        corrupt = b'{"version":1,"positions":'
        backup = dumps(ledger_v1(), target=1)
        guard._LEDGER_PATH.write_bytes(corrupt)
        guard._LEDGER_BAK_PATH.write_bytes(backup)
        with self.assertRaises(guard.LedgerError):
            guard.save_ledger(ledger_v1())
        self.assertEqual(guard._LEDGER_PATH.read_bytes(), corrupt)
        self.assertEqual(guard._LEDGER_BAK_PATH.read_bytes(), backup)

    def test_save_waits_for_exclusive_migration_fence(self):
        guard._LEDGER_PATH.write_bytes(dumps(ledger_v1(), target=1))
        started = threading.Event()
        finished = threading.Event()

        def writer() -> None:
            started.set()
            guard.save_ledger(ledger_v1())
            finished.set()

        with migration._exclusive(guard._LEDGER_MIGRATION_LOCK_PATH, 0.2):
            thread = threading.Thread(target=writer)
            thread.start()
            self.assertTrue(started.wait(0.2))
            time.sleep(0.05)
            self.assertFalse(finished.is_set())
        thread.join(1.0)
        self.assertTrue(finished.is_set())


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ownership_ledger.json"
        self.lock = self.root / ".migration.lock"
        self.archives = self.root / "archives"

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_refuses_and_dry_run_has_no_side_effects(self):
        with self.assertRaises(FileNotFoundError):
            migration.inspect(self.ledger)
        self.assertFalse(self.ledger.exists())

    def test_apply_and_cas_rollback(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        result = migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(loads_strict(self.ledger.read_bytes())["version"], 2)
        manifest = Path(str(result["manifest"]))
        rolled = migration.rollback(self.ledger, self.lock, manifest, 0.2)
        self.assertEqual(rolled["sha256"], migration._sha(original))
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_already_v2_apply_is_true_noop(self):
        self.ledger.write_bytes(dumps(ledger_v1(), target=2))
        before = self.ledger.stat().st_mtime_ns
        result = migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertTrue(result["already_v2"])
        self.assertEqual(self.ledger.stat().st_mtime_ns, before)
        self.assertFalse(self.archives.exists())

    def test_rollback_refuses_after_intervening_writer(self):
        self.ledger.write_bytes(dumps(ledger_v1(), target=1))
        result = migration.apply(self.ledger, self.lock, self.archives, 0.2)
        manifest = Path(str(result["manifest"]))
        self.ledger.write_bytes(self.ledger.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            migration.rollback(self.ledger, self.lock, manifest, 0.2)

    def test_post_replace_failure_automatically_restores_preimage(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        real_atomic = migration._atomic_bytes

        def fail_applied_manifest(path: Path, raw: bytes) -> None:
            if path.name.endswith(".manifest.json") and b'"status": "applied"' in raw:
                raise OSError("injected manifest-finalize failure")
            real_atomic(path, raw)

        with mock.patch.object(
            migration, "_atomic_bytes", side_effect=fail_applied_manifest
        ):
            with self.assertRaises(OSError):
                migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_post_replace_directory_fsync_failure_restores_preimage(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        real_fsync_dir = migration._fsync_dir
        failed = False

        def fail_first_ledger_dir(path: Path) -> None:
            nonlocal failed
            if path == self.root and not failed:
                failed = True
                raise OSError("injected ledger-directory fsync failure")
            real_fsync_dir(path)

        with mock.patch.object(
            migration, "_fsync_dir", side_effect=fail_first_ledger_dir
        ):
            with self.assertRaises(OSError):
                migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_target_replace_failure_leaves_preimage(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        real_replace = migration.os.replace

        def fail_target_replace(src: Path, dst: Path) -> None:
            if Path(dst) == self.ledger:
                raise OSError("injected target replace failure")
            real_replace(src, dst)

        with mock.patch.object(
            migration.os, "replace", side_effect=fail_target_replace
        ):
            with self.assertRaises(OSError):
                migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_prepared_manifest_failure_leaves_preimage(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        real_atomic = migration._atomic_bytes

        def fail_prepared(path: Path, raw: bytes) -> None:
            if path.name.endswith(".manifest.json"):
                raise OSError("injected prepared-manifest failure")
            real_atomic(path, raw)

        with mock.patch.object(migration, "_atomic_bytes", side_effect=fail_prepared):
            with self.assertRaises(OSError):
                migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_post_replace_reload_failure_restores_preimage(self):
        original = dumps(ledger_v1(), target=1)
        self.ledger.write_bytes(original)
        real_loads = migration.loads_strict

        def fail_v2_reload(raw: bytes | str) -> dict:
            parsed = real_loads(raw)
            if parsed.get("version") == 2:
                raise LedgerSchemaError("injected v2 reload failure")
            return parsed

        with mock.patch.object(migration, "loads_strict", side_effect=fail_v2_reload):
            with self.assertRaises(LedgerSchemaError):
                migration.apply(self.ledger, self.lock, self.archives, 0.2)
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_apply_refuses_when_migration_fence_is_busy(self):
        self.ledger.write_bytes(dumps(ledger_v1(), target=1))
        errors: list[Exception] = []

        def contender() -> None:
            try:
                migration.apply(self.ledger, self.lock, self.archives, 0.05)
            except Exception as exc:  # expected test capture
                errors.append(exc)

        with migration._exclusive(self.lock, 0.2):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(1.0)
        self.assertEqual(len(errors), 1)
        self.assertIn("lock unavailable", str(errors[0]))
        self.assertEqual(loads_strict(self.ledger.read_bytes())["version"], 1)

    def test_prepared_manifest_can_recover_visible_postimage(self):
        original = dumps(ledger_v1(), target=1)
        target = dumps(ledger_v1(), target=2)
        self.ledger.write_bytes(target)
        self.archives.mkdir()
        archive = self.archives / "preimage.json"
        archive.write_bytes(original)
        manifest = self.archives / "prepared.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "archive_path": str(archive),
                    "pre_sha256": migration._sha(original),
                    "post_sha256": migration._sha(target),
                }
            )
        )
        result = migration.rollback(self.ledger, self.lock, manifest, 0.2)
        self.assertEqual(result["sha256"], migration._sha(original))
        self.assertEqual(self.ledger.read_bytes(), original)

    def test_corrupt_archive_cannot_replace_visible_postimage(self):
        original = dumps(ledger_v1(), target=1)
        target = dumps(ledger_v1(), target=2)
        self.ledger.write_bytes(target)
        self.archives.mkdir()
        archive = self.archives / "preimage.json"
        archive.write_bytes(b"CORRUPT_ARCHIVE")
        manifest = self.archives / "prepared.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "archive_path": str(archive),
                    "pre_sha256": migration._sha(original),
                    "post_sha256": migration._sha(target),
                }
            )
        )
        with self.assertRaisesRegex(RuntimeError, "archive hash"):
            migration.rollback(self.ledger, self.lock, manifest, 0.2)
        self.assertEqual(self.ledger.read_bytes(), target)


if __name__ == "__main__":
    unittest.main()
