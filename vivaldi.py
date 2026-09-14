#!/usr/bin/env python3
"""Read-only, local command-line access to a Vivaldi profile on macOS."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
import heapq
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit


CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
DEFAULT_DATA_DIR = Path.home() / "Library/Application Support/Vivaldi"
VERSION = "0.2.0"


class VivaldiError(Exception):
    pass


def iso_time(value: int | str | None) -> str | None:
    if not value:
        return None
    try:
        microseconds = int(value)
        if microseconds == 0:
            return None
        return (CHROMIUM_EPOCH + timedelta(microseconds=microseconds)).astimezone().isoformat(timespec="seconds")
    except (ValueError, OverflowError, TypeError):
        return None


def domain_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def profiles(data_dir: Path) -> list[dict[str, str]]:
    state = data_dir / "Local State"
    if not state.is_file():
        raise VivaldiError(f"Vivaldi data not found at {data_dir}; open Vivaldi once or use --data-dir")
    try:
        cache = json.loads(state.read_text(encoding="utf-8"))["profile"]["info_cache"]
        if not isinstance(cache, dict) or any(not isinstance(item, dict) for item in cache.values()):
            raise ValueError("invalid profile cache")
        return [{"id": key, "name": value.get("name", key)} for key, value in cache.items()]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VivaldiError(f"Could not read profiles from {state}; check --data-dir") from exc


def selected_profiles(args: argparse.Namespace) -> list[dict[str, str]]:
    available = profiles(args.data_dir)
    if args.all_profiles:
        return available
    for item in available:
        if args.profile in (item["id"], item["name"]):
            return [item]
    names = ", ".join(item["id"] for item in available)
    raise VivaldiError(f"Profile '{args.profile}' not found. Available IDs: {names}")


@contextmanager
def history_connection(profile_dir: Path):
    source = profile_dir / "History"
    if not source.is_file():
        raise VivaldiError(f"History not found for profile: {profile_dir.name}")
    with TemporaryDirectory(prefix="vivaldi-cli-") as temporary:
        copy = Path(temporary) / "History"
        try:
            # Open the original read-only; the backup API keeps its WAL snapshot consistent.
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0.2)) as live:
                live.execute("PRAGMA schema_version").fetchone()
                last_progress = monotonic()

                def progress(status, _remaining, _total):
                    nonlocal last_progress
                    # SQLite's BUSY and LOCKED result codes are 5 and 6 (not named in Python 3.10).
                    if status in (5, 6):
                        if monotonic() - last_progress >= 2:
                            raise sqlite3.OperationalError("database is locked")
                    else:
                        last_progress = monotonic()

                with closing(sqlite3.connect(copy)) as snapshot:
                    live.backup(snapshot, pages=256, progress=progress, sleep=0.05)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            copy_exclusively_locked_history(source, copy)

        with closing(sqlite3.connect(copy.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            yield connection


def copy_exclusively_locked_history(source: Path, copy: Path) -> None:
    # Copy all SQLite state, then accept it only if the source stayed unchanged and recovery succeeds.
    source_files = (source, source.with_name("History-wal"), source.with_name("History-journal"))
    copy_files = (copy, copy.with_name("History-wal"), copy.with_name("History-journal"))

    def signature(path):
        try:
            state = path.stat()
        except FileNotFoundError:
            return None
        return state.st_ino, state.st_size, state.st_mtime_ns, state.st_ctime_ns

    for _ in range(3):
        before = tuple(signature(path) for path in source_files)
        if before[0] is None:
            raise VivaldiError(f"History not found for profile: {source.parent.name}")
        for path in copy_files[1:]:
            path.unlink(missing_ok=True)
        try:
            for original, destination, state in zip(source_files, copy_files, before):
                if state is not None:
                    shutil.copyfile(original, destination)
        except FileNotFoundError:
            continue
        if before != tuple(signature(path) for path in source_files):
            continue
        try:
            with closing(sqlite3.connect(copy)) as check:
                if check.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                    return
        except sqlite3.DatabaseError:
            continue
    raise VivaldiError("Could not make a stable History snapshot; retry when Vivaldi is idle")


def date_bounds(args: argparse.Namespace) -> tuple[int | None, int | None]:
    def to_chromium(day: date) -> int:
        local_midnight = datetime.combine(day, time.min).astimezone(timezone.utc)
        return int((local_midnight - CHROMIUM_EPOCH).total_seconds() * 1_000_000)

    try:
        start = date.fromisoformat(args.since) if args.since else None
        end = date.fromisoformat(args.until) if args.until else None
    except ValueError as exc:
        raise VivaldiError("Dates must use YYYY-MM-DD format") from exc
    if start and end and start > end:
        raise VivaldiError("--since must be earlier than or equal to --until")
    try:
        return to_chromium(start) if start else None, to_chromium(end + timedelta(days=1)) if end else None
    except OverflowError as exc:
        raise VivaldiError("Date is outside the supported range") from exc


def matches(value: str, query: str | None) -> bool:
    return not query or all(term.casefold() in value.casefold() for term in query.split())


def matches_domain(actual: str, requested: str | None) -> bool:
    return not requested or actual == requested.lower().removeprefix("www.")


def matches_folder(path: str, requested: str | None) -> bool:
    if not requested:
        return True
    folder = requested.strip("/").casefold()
    actual = path.casefold()
    return not folder or actual == folder or actual.startswith(folder + "/")


def merge_dated_rows(sources):
    sources = list(sources)
    try:
        for _, row in heapq.merge(*sources, key=lambda entry: entry[0], reverse=True):
            yield row
    finally:
        for source in sources:
            source.close()


def history_rows(args: argparse.Namespace):
    since, until = date_bounds(args)

    def from_profile(profile):
        with history_connection(args.data_dir / profile["id"]) as db:
            conditions, params = [], []
            if since is not None:
                conditions.append("v.visit_time >= ?")
                params.append(since)
            if until is not None:
                conditions.append("v.visit_time < ?")
                params.append(until)
            statement = """SELECT u.url, u.title, v.visit_time, u.visit_count,
                                  u.typed_count, v.visit_duration
                           FROM visits v JOIN urls u ON u.id = v.url"""
            if conditions:
                statement += " WHERE " + " AND ".join(conditions)
            statement += " ORDER BY v.visit_time DESC"
            for url, title, visited, visits, typed, duration in db.execute(statement, params):
                domain = domain_of(url)
                if not matches_domain(domain, args.domain):
                    continue
                if not matches(f"{title or ''} {url}", getattr(args, "query", None)):
                    continue
                yield visited, {"profile": profile["id"], "title": title or "", "url": url,
                                "domain": domain, "visited": iso_time(visited),
                                "visit_count": visits, "typed_count": typed,
                                "visit_duration_us": duration}

    yield from merge_dated_rows(from_profile(profile) for profile in selected_profiles(args))


def bookmark_rows(args: argparse.Namespace):
    for profile in selected_profiles(args):
        path = args.data_dir / profile["id"] / "Bookmarks"
        if not path.is_file():
            raise VivaldiError(f"Bookmarks not found for profile: {profile['id']}")
        try:
            roots = json.loads(path.read_text(encoding="utf-8"))["roots"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise VivaldiError(f"Could not read bookmarks for profile: {profile['id']}") from exc

        def walk(node: dict, folder: tuple[str, ...]):
            if node.get("type") == "folder":
                next_folder = folder + ((node.get("name") or ""),)
                for child in node.get("children", []):
                    yield from walk(child, next_folder)
            elif node.get("type") == "url":
                url = node.get("url", "")
                title = node.get("name", "")
                folder_name = "/".join(part for part in folder if part)
                domain = domain_of(url)
                if (matches(f"{title} {url} {folder_name}", args.query)
                        and matches_domain(domain, args.domain)
                        and matches_folder(folder_name, args.folder)):
                    yield {"profile": profile["id"], "title": title, "url": url,
                           "domain": domain, "folder": folder_name,
                           "added": iso_time(node.get("date_added"))}

        for root_name, root in roots.items():
            if root_name != "trash":
                yield from walk(root, ())


def download_rows(args: argparse.Namespace):
    since, until = date_bounds(args)

    def from_profile(profile):
        with history_connection(args.data_dir / profile["id"]) as db:
            conditions, params = [], []
            if since is not None:
                conditions.append("d.start_time >= ?")
                params.append(since)
            if until is not None:
                conditions.append("d.start_time < ?")
                params.append(until)
            statement = """SELECT d.target_path, d.start_time, d.end_time,
                                  d.received_bytes, d.total_bytes, d.state,
                                  (SELECT c.url FROM downloads_url_chains c
                                   WHERE c.id = d.id ORDER BY c.chain_index DESC LIMIT 1)
                           FROM downloads d"""
            if conditions:
                statement += " WHERE " + " AND ".join(conditions)
            statement += " ORDER BY d.start_time DESC"
            for path, started, ended, received, total, state, url in db.execute(statement, params):
                url = url or ""
                if not matches_domain(domain_of(url), args.domain):
                    continue
                if not matches(f"{path or ''} {url}", args.query):
                    continue
                yield started, {"profile": profile["id"], "filename": Path(path or "").name,
                                "path": path or "", "url": url, "domain": domain_of(url),
                                "started": iso_time(started), "finished": iso_time(ended),
                                "received_bytes": received, "total_bytes": total, "state": state}

    yield from merge_dated_rows(from_profile(profile) for profile in selected_profiles(args))


JXA_TABS = """const app = Application('Vivaldi');
const result = [];
const windows = app.windows();
for (let w = 0; w < windows.length; w++) {
  const tabs = windows[w].tabs();
  for (let t = 0; t < tabs.length; t++) {
    result.push({window: w + 1, tab: t + 1,
                 title: tabs[t].title(), url: tabs[t].url()});
  }
}
JSON.stringify(result);
"""


def tab_rows(args: argparse.Namespace):
    if sys.platform != "darwin":
        raise VivaldiError("Listing tabs requires macOS")
    try:
        output = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA_TABS],
                                capture_output=True, text=True, check=True, timeout=25)
        rows = json.loads(output.stdout)
    except subprocess.TimeoutExpired as exc:
        raise VivaldiError("Timed out listing tabs; check whether Vivaldi is responding") from exc
    except subprocess.CalledProcessError as exc:
        if "-1743" in (exc.stderr or ""):
            raise VivaldiError(
                "Automation access denied; allow your terminal to control Vivaldi "
                "in macOS Privacy & Security > Automation"
            ) from exc
        raise VivaldiError("Could not list tabs; open Vivaldi and allow Apple Events access") from exc
    except (OSError, ValueError) as exc:
        raise VivaldiError("Could not read tab data from Vivaldi; check that Vivaldi is open") from exc
    for row in rows:
        domain = domain_of(row["url"])
        if matches(f"{row['title']} {row['url']}", args.query) and matches_domain(domain, args.domain):
            row["domain"] = domain
            yield row


def stats(args: argparse.Namespace) -> dict:
    domains, days, hours = Counter(), Counter(), Counter()
    total, urls = 0, set()
    for row in history_rows(args):
        total += 1
        urls.add((row["profile"], row["url"]))
        if row["domain"]:
            domains[row["domain"]] += 1
        if row["visited"]:
            visit = datetime.fromisoformat(row["visited"])
            days[visit.date().isoformat()] += 1
            hours[f"{visit.hour:02d}"] += 1
    return {"visits": total, "unique_urls": len(urls),
            "top_domains": [{"domain": key, "visits": count}
                            for key, count in domains.most_common(args.top)],
            "by_day": dict(sorted(days.items())), "by_hour": dict(sorted(hours.items()))}


def show(rows, args: argparse.Namespace, kind: str) -> None:
    if kind == "stats":
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            print(f"Visits: {rows['visits']} | Unique URLs: {rows['unique_urls']}")
            for item in rows["top_domains"]:
                print(f"{item['visits']:>7}  {item['domain']}")
        return
    if kind == "profiles":
        selected = rows
    else:
        selected = []
        with closing(rows):
            for row in rows:
                selected.append(row)
                if args.limit and len(selected) >= args.limit:
                    break
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    else:
        for row in selected:
            if kind == "profiles":
                print(f"{row['id']}\t{row['name']}")
            elif kind == "tabs":
                print(f"{row['window']}:{row['tab']}\t{row['title']}\t{row['url']}")
            elif kind == "downloads":
                print(f"{row['started']}\t{row['filename']}\t{row['url']}")
            else:
                stamp = row.get("visited") or row.get("added") or ""
                print(f"{stamp}\t{row['title']}\t{row['url']}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vivaldi", description="Read-only local access to Vivaldi data")
    root.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = root.add_subparsers(dest="command", required=True)

    def common(command, *, profile=True, dates=False, domain=False, folder=False,
               query=False, query_help="terms in title or URL", limit=True):
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                             help="Vivaldi data directory (default: macOS profile directory)")
        command.add_argument("--json", action="store_true", help="JSON output for scripts")
        if profile:
            selected = command.add_mutually_exclusive_group()
            selected.add_argument("--profile", default="Default", help="profile ID or name")
            selected.add_argument("--all-profiles", action="store_true", help="all local profiles")
        if query:
            command.add_argument("query", nargs="?", help=query_help)
        if dates:
            command.add_argument("--since", help="start date in local time, YYYY-MM-DD")
            command.add_argument("--until", help="end date in local time, YYYY-MM-DD (inclusive)")
        if domain:
            command.add_argument("--domain", help="exact domain, without www")
        if folder:
            command.add_argument("--folder", help="bookmark folder path, including subfolders")
        if limit:
            command.add_argument("--limit", type=int, default=50, help="maximum results; 0 = all")

    common(sub.add_parser("profiles", help="list profiles"), profile=False, limit=False)
    common(sub.add_parser("history", help="search visits"), dates=True, domain=True, query=True)
    common(sub.add_parser("bookmarks", help="search bookmarks"), domain=True, folder=True,
           query=True, query_help="terms in title, URL, or folder")
    common(sub.add_parser("downloads", help="search downloads"), dates=True, domain=True,
           query=True, query_help="terms in file path or URL")
    common(sub.add_parser("tabs", help="list open tabs"), profile=False, domain=True, query=True)
    stats_parser = sub.add_parser("stats", help="statistics for available history")
    common(stats_parser, dates=True, domain=True, limit=False)
    stats_parser.add_argument("--top", type=int, default=10, help="number of top domains")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if hasattr(args, "limit") and args.limit < 0:
        raise SystemExit("--limit cannot be negative")
    if hasattr(args, "top") and args.top < 0:
        raise SystemExit("--top cannot be negative")
    try:
        commands = {"profiles": lambda: profiles(args.data_dir),
                    "history": lambda: history_rows(args),
                    "bookmarks": lambda: bookmark_rows(args),
                    "downloads": lambda: download_rows(args),
                    "tabs": lambda: tab_rows(args),
                    "stats": lambda: stats(args)}
        show(commands[args.command](), args, args.command)
        return 0
    except (VivaldiError, OSError, sqlite3.Error) as exc:
        print(f"vivaldi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
