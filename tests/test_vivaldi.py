import json
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import os
import struct
from time import monotonic, sleep

import bookmark_bridge


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import vivaldi  # noqa: E402


class VivaldiCLITest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        (self.data_dir / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {
                "Default": {"name": "Personal"},
                "Profile 1": {"name": "Work"},
            }}
        }), encoding="utf-8")
        for profile in ("Default", "Profile 1"):
            directory = self.data_dir / profile
            directory.mkdir()
            (directory / "Bookmarks").write_text(json.dumps({"roots": {
                "bookmark_bar": {"type": "folder", "id": "1", "name": "Bar", "children": [
                    {"type": "url", "id": "10", "name": "Example", "url": "https://example.com/page",
                     "date_added": "0"}]},
                "other": {"type": "folder", "id": "2", "name": "Menu", "children": [
                    {"type": "folder", "id": "3", "name": "Projects", "children": [
                        {"type": "url", "id": "11", "name": "Other", "url": "https://other.test/page"}]}]},
                "trash": {"type": "folder", "id": "4", "name": "Trash", "children": [
                    {"type": "url", "id": "12", "name": "Deleted", "url": "https://deleted.test"}]},
            }}), encoding="utf-8")
            db = sqlite3.connect(directory / "History")
            db.executescript("""
                CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT,
                                   visit_count INTEGER, typed_count INTEGER);
                CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER,
                                     visit_time INTEGER, visit_duration INTEGER);
                CREATE TABLE downloads (id INTEGER PRIMARY KEY, target_path TEXT,
                                        start_time INTEGER, end_time INTEGER,
                                        received_bytes INTEGER, total_bytes INTEGER, state INTEGER);
                CREATE TABLE downloads_url_chains (id INTEGER, chain_index INTEGER, url TEXT);
            """)
            visit_time = 13300000000000000 + (1000000 if profile == "Profile 1" else 0)
            db.execute("INSERT INTO urls VALUES (1, 'https://example.com/page', 'Example Page', 2, 1)")
            db.execute("INSERT INTO visits VALUES (1, 1, ?, 1000000)", (visit_time,))
            db.execute("INSERT INTO downloads VALUES (1, '/tmp/report.pdf', ?, ?, 10, 10, 1)",
                       (visit_time, visit_time + 1000000))
            db.execute("INSERT INTO downloads_url_chains VALUES (1, 0, 'https://example.com/report.pdf')")
            db.commit()
            db.close()

    def run_cli(self, *args):
        completed = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), *args,
                                    "--data-dir", str(self.data_dir), "--json"],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_version_does_not_need_a_profile(self):
        result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "--version"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"vivaldi {vivaldi.VERSION}")

    def test_help_and_stats_text_are_english(self):
        help_result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "--help"],
                                     capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("Read-only local access to Vivaldi data", help_result.stdout)
        self.assertIn("search bookmarks", help_result.stdout)
        self.assertIn("insights", help_result.stdout)

        stats_result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "stats",
                                       "--data-dir", str(self.data_dir)], capture_output=True, text=True)
        self.assertEqual(stats_result.returncode, 0, stats_result.stderr)
        self.assertIn("Visits: 1 | Unique URLs: 1", stats_result.stdout)

    def test_profiles_and_explicit_profile(self):
        self.assertEqual(len(self.run_cli("profiles")), 2)
        rows = self.run_cli("history", "example.com", "--profile", "Work")
        self.assertEqual([row["profile"] for row in rows], ["Profile 1"])

    def test_default_profile_is_visible_without_changing_json(self):
        command = [sys.executable, str(SOURCE / "vivaldi.py"), "history", "example.com",
                   "--data-dir", str(self.data_dir), "--json"]
        implicit = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(implicit.returncode, 0, implicit.stderr)
        self.assertEqual([row["profile"] for row in json.loads(implicit.stdout)], ["Default"])
        self.assertIn("Using profile Default", implicit.stderr)
        explicit = subprocess.run(command + ["--profile", "Default"],
                                  capture_output=True, text=True)
        self.assertEqual(explicit.returncode, 0, explicit.stderr)
        self.assertEqual(explicit.stderr, "")
        self.assertIn("default: Default", subprocess.run(
            [sys.executable, str(SOURCE / "vivaldi.py"), "history", "--help"],
            capture_output=True, text=True).stdout)

    def test_all_profiles_text_identifies_origin_and_bookmark_folder(self):
        for command in (("history", "example.com"), ("bookmarks", "Example"),
                        ("downloads", "report.pdf"), ("bookmark", "folders"),
                        ("search", "example.com")):
            result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), *command,
                                     "--all-profiles", "--data-dir", str(self.data_dir)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Default\t", result.stdout)
            if command[0] == "search":
                self.assertIn("Profile 1,Default\t", result.stdout)
            else:
                self.assertIn("Profile 1\t", result.stdout)
            if command[0] == "bookmarks":
                self.assertIn("Default\t10\tBar\tExample\t", result.stdout)

    def test_history_filter_preserves_unicode_and_cross_field_matching(self):
        with closing(sqlite3.connect(self.data_dir / "Default" / "History")) as db:
            with db:
                db.execute("INSERT INTO urls VALUES (2, 'https://other.test/straße', 'Coffee', 1, 0)")
                db.execute("INSERT INTO visits VALUES (2, 2, 13300000000000001, 0)")
        rows = self.run_cli("history", "COFFEE STRASSE", "--domain", "www.other.test")
        self.assertEqual([row["url"] for row in rows], ["https://other.test/straße"])

    def test_history_filters_and_stats(self):
        rows = self.run_cli("history", "Example", "--all-profiles", "--domain", "example.com")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["profile"], "Profile 1")
        self.assertEqual(self.run_cli("history", "--all-profiles", "--limit", "1")[0]["profile"], "Profile 1")
        self.assertEqual(self.run_cli("history", "--since", "2025-01-01"), [])
        summary = self.run_cli("stats", "--all-profiles")
        self.assertEqual(summary["visits"], 2)
        self.assertEqual(summary["top_domains"], [{"domain": "example.com", "visits": 2}])

    def test_report_compares_periods_and_audits_duration_without_summing_time(self):
        def chromium_day(day):
            instant = datetime.combine(day, time.min).astimezone(timezone.utc)
            return int((instant - vivaldi.CHROMIUM_EPOCH).total_seconds() * 1_000_000)

        today = date.today()
        with closing(sqlite3.connect(self.data_dir / "Default" / "History")) as db:
            with db:
                db.execute("INSERT INTO visits VALUES (2, 1, ?, 0)", (chromium_day(today),))
                db.execute("INSERT INTO visits VALUES (3, 1, ?, NULL)",
                           (chromium_day(today - timedelta(days=1)),))
                db.execute("INSERT INTO visits VALUES (4, 1, ?, 5000000)",
                           (chromium_day(today - timedelta(days=30)),))
                db.execute("INSERT INTO visits VALUES (5, 1, ?, -1)",
                           (chromium_day(today - timedelta(days=2)),))
        report = self.run_cli("report", "--days", "30")
        self.assertEqual(report["current"]["visits"], 3)
        self.assertEqual(report["previous"]["visits"], 1)
        self.assertEqual(report["change"]["visits"], 2)
        self.assertTrue(report["today_incomplete"])
        quality = report["current"]["duration_quality"]
        self.assertEqual({key: quality[key] for key in ("missing", "invalid", "zero", "positive")},
                         {"missing": 1, "invalid": 1, "zero": 1, "positive": 0})
        self.assertNotIn("total_us", quality)
        args = vivaldi.parser().parse_args(["report", "--data-dir", str(self.data_dir)])
        prior_day = today - timedelta(days=30)
        positive = vivaldi.report_period(args, prior_day, prior_day, audit=True)["duration_quality"]
        self.assertEqual((positive["positive"], positive["median_us"], positive["p95_us"]),
                         (1, 5000000, 5000000))
        empty = vivaldi.report_period(args, date(2100, 1, 1), date(2100, 1, 1), audit=True)
        self.assertEqual(empty["visits"], 0)
        self.assertEqual(empty["by_day"], {"2100-01-01": 0})
        self.assertEqual(len(empty["by_hour"]), 24)
        self.assertEqual(empty["duration_quality"]["positive_percent"], 0.0)

    def test_report_counts_distinct_urls_across_profiles(self):
        today = datetime.combine(date.today(), time.min).astimezone(timezone.utc)
        visit_time = int((today - vivaldi.CHROMIUM_EPOCH).total_seconds() * 1_000_000)
        for profile in ("Default", "Profile 1"):
            with closing(sqlite3.connect(self.data_dir / profile / "History")) as db:
                with db:
                    db.execute("INSERT INTO visits VALUES (2, 1, ?, 1000000)", (visit_time,))
        summary = self.run_cli("report", "--all-profiles")
        self.assertEqual(summary["current"]["visits"], 2)
        self.assertEqual(summary["current"]["unique_urls"], 1)

    def test_insights_compares_domains_revisits_and_concentration(self):
        today = date.today()

        def visit_time(days_ago, hour):
            local = datetime.combine(today - timedelta(days=days_ago), time(hour))
            return int((local.astimezone(timezone.utc) - vivaldi.CHROMIUM_EPOCH).total_seconds() * 1_000_000)

        with closing(sqlite3.connect(self.data_dir / "Default" / "History")) as db:
            with db:
                db.execute("INSERT INTO urls VALUES (2, 'https://new.test/page', 'New', 2, 0)")
                db.execute("INSERT INTO urls VALUES (3, 'https://gone.test/page', 'Gone', 1, 0)")
                for identifier, url_id, days_ago, hour in (
                    (2, 1, 0, 9), (3, 1, 0, 10), (4, 1, 1, 9),
                    (5, 2, 2, 14), (6, 2, 3, 18),
                    (7, 1, 4, 9), (8, 3, 5, 17),
                ):
                    db.execute("INSERT INTO visits VALUES (?, ?, ?, 0)",
                               (identifier, url_id, visit_time(days_ago, hour)))
        result = self.run_cli("insights", "--days", "4")
        self.assertEqual((result["current"]["visits"], result["previous"]["visits"]), (5, 2))
        self.assertEqual(result["current"]["start"], (today - timedelta(days=3)).isoformat())
        self.assertEqual(result["previous"]["end"], (today - timedelta(days=4)).isoformat())
        self.assertEqual(result["change"], {"visits": 3, "unique_urls": 0})
        self.assertEqual(result["domains"]["new_vs_previous"], {
            "count": 1, "visits": 2, "top": [{"domain": "new.test", "visits": 2}]})
        self.assertEqual(result["domains"]["recurring"], {"count": 1, "visits": 3})
        self.assertEqual(result["domains"]["largest_decreases"][0]["domain"], "gone.test")
        self.assertEqual(result["revisits"]["urls_visited_multiple_times"], 2)
        self.assertEqual(result["revisits"]["repeat_visits"], 3)
        self.assertEqual(result["revisits"]["top"][0],
                         {"url": "https://example.com/page", "visits": 3})
        self.assertEqual(result["concentration"]["active_days"], 4)
        self.assertEqual(result["concentration"]["top_3_days_share_percent"], 80.0)
        self.assertEqual(result["concentration"]["top_3_hours_share_percent"], 80.0)
        self.assertTrue(result["today_incomplete"])
        self.assertNotIn("hours_by_site", result)

        with closing(sqlite3.connect(self.data_dir / "Profile 1" / "History")) as db:
            with db:
                db.execute("INSERT INTO visits VALUES (2, 1, ?, 0)", (visit_time(0, 11),))
        all_profiles = self.run_cli("insights", "--days", "4", "--all-profiles", "--top", "0")
        self.assertEqual(all_profiles["current"]["visits"], 6)
        self.assertEqual(all_profiles["current"]["unique_urls"], 2)
        self.assertEqual(all_profiles["revisits"]["repeat_visits"], 4)
        self.assertEqual(all_profiles["revisits"]["top"], [])
        self.assertEqual(all_profiles["domains"]["new_vs_previous"]["count"], 1)

    def test_insights_empty_period_and_english_text(self):
        empty = self.run_cli("insights", "--days", "1")
        self.assertEqual(empty["current"]["visits"], 0)
        self.assertEqual(empty["domains"]["new_vs_previous"]["count"], 0)
        self.assertEqual(empty["revisits"]["repeat_visits"], 0)
        self.assertEqual(empty["concentration"]["top_3_hours_share_percent"], 0.0)
        text_result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "insights",
                                      "--days", "1", "--data-dir", str(self.data_dir)],
                                     capture_output=True, text=True)
        self.assertEqual(text_result.returncode, 0, text_result.stderr)
        self.assertIn("Recorded visits: 0", text_result.stdout)
        self.assertIn("not active browsing time", text_result.stdout)

    def test_search_deduplicates_exact_urls_and_handles_empty_results(self):
        rows = self.run_cli("search", "example", "--all-profiles", "--limit", "0")
        page = next(row for row in rows if row["url"] == "https://example.com/page")
        self.assertEqual({match["source"] for match in page["matches"]}, {"history", "bookmarks"})
        self.assertEqual(len(page["matches"]), 4)
        self.assertEqual(self.run_cli("search", "nothing-here"), [])
        self.assertEqual(len(self.run_cli("search", "example", "--limit", "1")), 1)

    def test_search_includes_all_provenance_and_counts_repeated_downloads(self):
        with closing(sqlite3.connect(self.data_dir / "Default" / "History")) as db:
            with db:
                db.execute("INSERT INTO urls VALUES (2, 'https://other.test/page', 'Unrelated', 1, 0)")
                db.execute("INSERT INTO visits VALUES (3, 2, 13300000000000000, 0)")
                for identifier in (2, 3):
                    db.execute("INSERT INTO downloads VALUES (?, '/tmp/other.bin', 13300000000000000, "
                               "13300000000000000, 10, 10, 1)", (identifier,))
                    db.execute("INSERT INTO downloads_url_chains VALUES (?, 0, "
                               "'https://other.test/page')", (identifier,))
        rows = self.run_cli("search", "Projects", "--limit", "0")
        matches = next(row for row in rows if row["url"] == "https://other.test/page")["matches"]
        self.assertEqual({match["source"] for match in matches}, {"bookmarks", "history", "downloads"})
        self.assertEqual(next(match for match in matches if match["source"] == "downloads")["downloads"], 2)

    def test_bookmark_ids_and_folder_listing(self):
        self.assertEqual(self.run_cli("bookmarks", "Example")[0]["id"], "10")
        folders = self.run_cli("bookmark", "folders")
        self.assertEqual([(row["id"], row["path"]) for row in folders],
                         [("1", "Bar"), ("2", "Menu"), ("3", "Menu/Projects")])

    def test_bookmark_preview_apply_and_stale_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"VIVALDI_CLI_BRIDGE_DIR": temporary,
                                      "VIVALDI_CLI_MANIFEST_DIR": temporary}):
                bookmark_bridge.setup(self.data_dir, "Default", "a" * 32, "b" * 32,
                                      SOURCE / "bridge_host.py")
                item = {"id": "10", "parentId": "1", "title": "Example",
                        "url": "https://example.com/page", "unmodifiable": None}
                destination = {"id": "3", "parentId": "2", "title": "Projects",
                               "url": None, "unmodifiable": None}
                calls = []

                def fake_request(message, **_kwargs):
                    calls.append(message)
                    if message["op"] == "ping":
                        return {"ok": True, "pairing_code": "b" * 32}
                    if message["op"] == "inspect":
                        return {"ok": True, "snapshot": {"item": item, "destination": destination}}
                    return {"ok": True, "after": {**item, "parentId": "3"}}

                with patch.object(bookmark_bridge, "request", side_effect=fake_request):
                    args = vivaldi.parser().parse_args(["bookmark", "move", "10", "--to", "3",
                                                       "--data-dir", str(self.data_dir)])
                    preview = vivaldi.bookmark_operation(args)
                    self.assertEqual(preview["status"], "preview")
                    self.assertEqual([call["op"] for call in calls], ["ping", "inspect"])
                    config = bookmark_bridge.load_config()
                    operation = calls[-1]["operation"]
                    snapshot = {"item": item, "destination": destination}
                    with self.assertRaisesRegex(bookmark_bridge.BridgeError, "preview token"):
                        bookmark_bridge.authorized_message(config, {
                            "op": "execute", "operation": operation, "expected": snapshot})
                    with self.assertRaisesRegex(bookmark_bridge.BridgeError, "trash guard"):
                        bookmark_bridge.authorized_message(config, {
                            "op": "execute", "operation": {**operation, "trash": "wrong"},
                            "expected": snapshot, "token": preview["token"]})
                    other_profile = vivaldi.parser().parse_args(["bookmark", "move", "10", "--to", "3",
                                                                 "--profile", "Work", "--data-dir", str(self.data_dir)])
                    with self.assertRaisesRegex(vivaldi.VivaldiError, "not paired"):
                        vivaldi.bookmark_operation(other_profile)
                    self.assertEqual([call["op"] for call in calls], ["ping", "inspect"])
                    args.apply = "wrong"
                    with self.assertRaisesRegex(vivaldi.VivaldiError, "token is invalid"):
                        vivaldi.bookmark_operation(args)
                    self.assertNotIn("execute", [call["op"] for call in calls])
                    args.apply = preview["token"]
                    self.assertEqual(vivaldi.bookmark_operation(args)["status"], "applied")
                    self.assertEqual(calls[-1]["op"], "execute")
                    self.assertEqual(calls[-1]["token"], preview["token"])
                    item["title"] = "Changed since preview"
                    with self.assertRaisesRegex(vivaldi.VivaldiError, "state changed"):
                        vivaldi.bookmark_operation(args)

    def test_native_host_round_trip_and_bridge_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {"VIVALDI_CLI_BRIDGE_DIR": temporary,
                           "VIVALDI_CLI_MANIFEST_DIR": temporary}
            with patch.dict(os.environ, environment):
                with self.assertRaisesRegex(bookmark_bridge.BridgeError, "unavailable"):
                    bookmark_bridge.request({"op": "ping"}, timeout=0.1)
                bookmark_bridge.setup(self.data_dir, "Default", "a" * 32, "b" * 32,
                                      SOURCE / "bridge_host.py")
                manifest = json.loads(bookmark_bridge.manifest_path().read_text())
                self.assertEqual(manifest["allowed_origins"], ["chrome-extension://" + "a" * 32 + "/"])
                process = subprocess.Popen([manifest["path"], "chrome-extension://" + "a" * 32 + "/"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env={**os.environ, **environment})
                try:
                    deadline = monotonic() + 5
                    while not bookmark_bridge.socket_path().exists() and monotonic() < deadline:
                        sleep(0.02)
                    self.assertTrue(bookmark_bridge.socket_path().exists(),
                                    process.stderr.read().decode() if process.poll() is not None else "")
                    with ThreadPoolExecutor(max_workers=1) as workers:
                        future = workers.submit(bookmark_bridge.request, {"op": "ping"})
                        length = struct.unpack("=I", bookmark_bridge.read_exact(process.stdout, 4))[0]
                        message = json.loads(bookmark_bridge.read_exact(process.stdout, length))
                        self.assertEqual(message, {"op": "ping"})
                        reply = json.dumps({"ok": True, "pairing_code": "b" * 32}).encode()
                        process.stdin.write(struct.pack("=I", len(reply)) + reply)
                        process.stdin.flush()
                        self.assertEqual(future.result(timeout=5)["pairing_code"], "b" * 32)
                    duplicate = subprocess.run([manifest["path"], "chrome-extension://" + "a" * 32 + "/"],
                                               input=b"", capture_output=True, env={**os.environ, **environment},
                                               timeout=5)
                    self.assertNotEqual(duplicate.returncode, 0)
                    self.assertIn(b"Another bridge is already connected", duplicate.stderr)
                    self.assertTrue(bookmark_bridge.socket_path().exists())
                    with self.assertRaisesRegex(bookmark_bridge.BridgeError, "preview token"):
                        bookmark_bridge.request({"op": "execute", "operation": {
                            "kind": "edit", "id": "10", "title": "No change", "trash": "4"},
                            "expected": {"item": {"id": "10"}}})
                    operation = {"kind": "edit", "id": "10", "title": "No change", "trash": "4"}
                    expected = {"item": {"id": "10"}}
                    supplied = bookmark_bridge.token(bookmark_bridge.load_config(), operation, expected)
                    with ThreadPoolExecutor(max_workers=1) as workers:
                        future = workers.submit(bookmark_bridge.request, {
                            "op": "execute", "operation": operation, "expected": expected,
                            "token": supplied})
                        length = struct.unpack("=I", bookmark_bridge.read_exact(process.stdout, 4))[0]
                        self.assertEqual(json.loads(bookmark_bridge.read_exact(process.stdout, length)), {"op": "ping"})
                        wrong_pair = json.dumps({"ok": True, "pairing_code": "wrong"}).encode()
                        process.stdin.write(struct.pack("=I", len(wrong_pair)) + wrong_pair)
                        process.stdin.flush()
                        with self.assertRaisesRegex(bookmark_bridge.BridgeError, "paired profile"):
                            future.result(timeout=5)
                    with ThreadPoolExecutor(max_workers=1) as workers:
                        future = workers.submit(bookmark_bridge.request, {"op": "ping"})
                        length = struct.unpack("=I", bookmark_bridge.read_exact(process.stdout, 4))[0]
                        self.assertEqual(json.loads(bookmark_bridge.read_exact(process.stdout, length)), {"op": "ping"})
                        process.stdin.write(struct.pack("=I", len(reply)) + reply)
                        process.stdin.flush()
                        self.assertEqual(future.result(timeout=5)["pairing_code"], "b" * 32)
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)
                    process.stdin.close()
                    process.stdout.close()
                    process.stderr.close()

    def test_bridge_manifest_follows_custom_user_data_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"VIVALDI_CLI_BRIDGE_DIR": temporary,
                                      "VIVALDI_CLI_MANIFEST_DIR": ""}):
                result = bookmark_bridge.setup(self.data_dir, "Default", "a" * 32,
                                               "b" * 32, SOURCE / "bridge_host.py")
                path = self.data_dir / "NativeMessagingHosts" / "com.vivaldi_cli.bookmarks.json"
                self.assertEqual(result["manifest"], str(path))
                self.assertTrue(path.is_file())

    def test_history_with_exclusive_source_lock(self):
        connection = sqlite3.connect(self.data_dir / "Default" / "History")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self.assertEqual(len(self.run_cli("history", "Example")), 1)
        finally:
            connection.rollback()
            connection.close()

    def test_history_snapshot_excludes_uncommitted_write(self):
        source = self.data_dir / "Default" / "History"
        connection = sqlite3.connect(source)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("INSERT INTO urls VALUES (2, 'https://pending.test', 'Pending', 1, 0)")
            self.assertTrue(source.with_name("History-journal").exists())
            result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "history", "--json",
                                     "--data-dir", str(self.data_dir)], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([row["domain"] for row in json.loads(result.stdout)], ["example.com"])
        finally:
            connection.rollback()
            connection.close()

    def test_history_rejects_source_changed_during_fallback_copy(self):
        source = self.data_dir / "Default" / "History"
        original_copyfile = vivaldi.shutil.copyfile

        def copy_and_change(original, destination):
            original_copyfile(original, destination)
            with source.open("ab") as handle:
                handle.write(b"x")

        with patch.object(vivaldi.shutil, "copyfile", side_effect=copy_and_change):
            with self.assertRaisesRegex(vivaldi.VivaldiError, "stable History snapshot"):
                vivaldi.copy_exclusively_locked_history(source, self.data_dir / "snapshot")

    def test_history_with_active_wal(self):
        source = self.data_dir / "Default" / "History"
        connection = sqlite3.connect(source)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("INSERT INTO urls VALUES (2, 'https://new.test/page', 'New Page', 1, 0)")
            connection.execute("INSERT INTO visits VALUES (2, 2, 13300000002000000, 1000000)")
            connection.commit()
            self.assertTrue(source.with_name("History-wal").exists())
            rows = self.run_cli("history", "new.test")
            self.assertEqual([row["url"] for row in rows], ["https://new.test/page"])
            with patch.object(vivaldi.shutil, "copyfile", side_effect=AssertionError("raw copy used")):
                with vivaldi.history_connection(self.data_dir / "Default") as snapshot:
                    self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM visits").fetchone()[0], 2)
        finally:
            connection.close()

    def test_bookmarks_exclude_trash_and_downloads(self):
        bookmarks = self.run_cli("bookmarks", "Bar")
        self.assertEqual(len(bookmarks), 1)
        self.assertEqual(bookmarks[0]["folder"], "Bar")
        self.assertIsNone(bookmarks[0]["added"])
        self.assertEqual(self.run_cli("bookmarks", "Deleted"), [])
        self.assertEqual([row["domain"] for row in self.run_cli("bookmarks", "--domain", "www.example.com")],
                         ["example.com"])
        self.assertEqual([row["folder"] for row in self.run_cli("bookmarks", "--folder", "Menu")],
                         ["Menu/Projects"])
        self.assertEqual([row["domain"] for row in self.run_cli("bookmarks", "--folder", "menu/projects",
                                                                   "--domain", "other.test")], ["other.test"])
        self.assertEqual(self.run_cli("bookmarks", "--folder", "Projects"), [])
        downloads = self.run_cli("downloads", "report", "--domain", "example.com")
        self.assertEqual(downloads[0]["filename"], "report.pdf")
        self.assertEqual(downloads[0]["url"], "https://example.com/report.pdf")
        self.assertEqual(self.run_cli("downloads", "--all-profiles", "--limit", "1")[0]["profile"], "Profile 1")

    def test_bad_dates_and_profile_metadata_return_errors(self):
        command = [sys.executable, str(SOURCE / "vivaldi.py"), "history", "--until", "9999-12-31",
                   "--data-dir", str(self.data_dir)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Date is outside the supported range", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        (self.data_dir / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {"Default": None}}
        }), encoding="utf-8")
        result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "profiles",
                                 "--data-dir", str(self.data_dir)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not read profiles", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_profile_directory_suggests_recovery(self):
        result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "profiles",
                                 "--data-dir", str(self.data_dir / "missing")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("open Vivaldi once or use --data-dir", result.stderr)

    def test_tabs_parse_jxa_and_filter(self):
        output = subprocess.CompletedProcess([], 0, stdout='[{"title":"Example","url":"https://example.com","window":1,"tab":1},'
                                                          '{"title":"Other","url":"https://other.test","window":1,"tab":2}]')
        args = vivaldi.parser().parse_args(["tabs", "example.com"])
        with patch.object(vivaldi.subprocess, "run", return_value=output):
            self.assertEqual(list(vivaldi.tab_rows(args))[0]["domain"], "example.com")
            filtered = vivaldi.parser().parse_args(["tabs", "--domain", "www.other.test"])
            self.assertEqual([row["title"] for row in vivaldi.tab_rows(filtered)], ["Other"])

    def test_tabs_report_permission_and_timeout(self):
        args = vivaldi.parser().parse_args(["tabs"])
        denied = subprocess.CalledProcessError(1, ["osascript"], stderr="Apple event error -1743")
        with patch.object(vivaldi.subprocess, "run", side_effect=denied):
            with self.assertRaisesRegex(vivaldi.VivaldiError, "Automation access denied"):
                list(vivaldi.tab_rows(args))
        with patch.object(vivaldi.subprocess, "run", side_effect=subprocess.TimeoutExpired(["osascript"], 25)):
            with self.assertRaisesRegex(vivaldi.VivaldiError, "Timed out listing tabs"):
                list(vivaldi.tab_rows(args))


if __name__ == "__main__":
    unittest.main()
