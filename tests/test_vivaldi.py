import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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
                "bookmark_bar": {"type": "folder", "name": "Bar", "children": [
                    {"type": "url", "name": "Example", "url": "https://example.com/page",
                     "date_added": "0"}]},
                "trash": {"type": "folder", "name": "Trash", "children": [
                    {"type": "url", "name": "Deleted", "url": "https://deleted.test"}]},
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

    def test_profiles_and_explicit_profile(self):
        self.assertEqual(len(self.run_cli("profiles")), 2)
        rows = self.run_cli("history", "example.com", "--profile", "Work")
        self.assertEqual([row["profile"] for row in rows], ["Profile 1"])

    def test_history_filters_and_stats(self):
        rows = self.run_cli("history", "Example", "--all-profiles", "--domain", "example.com")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["profile"], "Profile 1")
        self.assertEqual(self.run_cli("history", "--all-profiles", "--limit", "1")[0]["profile"], "Profile 1")
        self.assertEqual(self.run_cli("history", "--since", "2025-01-01"), [])
        summary = self.run_cli("stats", "--all-profiles")
        self.assertEqual(summary["visits"], 2)
        self.assertEqual(summary["top_domains"], [{"domain": "example.com", "visits": 2}])

    def test_bookmarks_exclude_trash_and_downloads(self):
        bookmarks = self.run_cli("bookmarks", "Bar")
        self.assertEqual(len(bookmarks), 1)
        self.assertEqual(bookmarks[0]["folder"], "Bar")
        self.assertIsNone(bookmarks[0]["added"])
        self.assertEqual(self.run_cli("bookmarks", "Deleted"), [])
        downloads = self.run_cli("downloads", "report", "--domain", "example.com")
        self.assertEqual(downloads[0]["filename"], "report.pdf")
        self.assertEqual(downloads[0]["url"], "https://example.com/report.pdf")
        self.assertEqual(self.run_cli("downloads", "--all-profiles", "--limit", "1")[0]["profile"], "Profile 1")

    def test_bad_dates_and_profile_metadata_return_errors(self):
        command = [sys.executable, str(SOURCE / "vivaldi.py"), "history", "--until", "9999-12-31",
                   "--data-dir", str(self.data_dir)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Data fora do intervalo", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        (self.data_dir / "Local State").write_text(json.dumps({
            "profile": {"info_cache": {"Default": None}}
        }), encoding="utf-8")
        result = subprocess.run([sys.executable, str(SOURCE / "vivaldi.py"), "profiles",
                                 "--data-dir", str(self.data_dir)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Não foi possível ler os perfis", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_tabs_parse_jxa_and_filter(self):
        output = subprocess.CompletedProcess([], 0, stdout='[{"title":"Example","url":"https://example.com","window":1,"tab":1}]')
        args = vivaldi.parser().parse_args(["tabs", "example.com"])
        with patch.object(vivaldi.subprocess, "run", return_value=output):
            self.assertEqual(list(vivaldi.tab_rows(args))[0]["domain"], "example.com")


if __name__ == "__main__":
    unittest.main()
