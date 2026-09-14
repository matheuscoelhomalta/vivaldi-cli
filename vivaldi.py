#!/usr/bin/env python3
"""Local command-line access to Vivaldi data on macOS."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
import heapq
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit

import bookmark_bridge as bridge


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
    requested = "Default" if args.profile is None else args.profile
    for item in available:
        if requested in (item["id"], item["name"]):
            return [item]
    names = ", ".join(item["id"] for item in available)
    raise VivaldiError(f"Profile '{requested}' not found. Available IDs: {names}")


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
            if args.domain or getattr(args, "query", None):
                db.create_function("cli_url_matches", 2, lambda url, title: int(
                    matches_domain(domain_of(url), args.domain) and
                    matches(f"{title or ''} {url}", getattr(args, "query", None))))
                conditions.append("u.id IN (SELECT id FROM urls WHERE cli_url_matches(url, title))")
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
                           "id": node.get("id"),
                           "domain": domain, "folder": folder_name,
                           "added": iso_time(node.get("date_added"))}

        for root_name, root in roots.items():
            if root_name != "trash":
                yield from walk(root, ())


def folder_rows(args: argparse.Namespace):
    for profile in selected_profiles(args):
        path = args.data_dir / profile["id"] / "Bookmarks"
        try:
            roots = json.loads(path.read_text(encoding="utf-8"))["roots"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise VivaldiError(f"Could not read bookmark folders for profile: {profile['id']}") from exc

        def walk(node, parent_id, parts):
            if node.get("type") != "folder":
                return
            name = node.get("name") or ""
            path_parts = parts + (name,)
            yield {"profile": profile["id"], "id": node.get("id"), "parent_id": parent_id,
                   "name": name, "path": "/".join(path_parts)}
            for child in node.get("children", []):
                yield from walk(child, node.get("id"), path_parts)

        for root_name, root in roots.items():
            if root_name != "trash":
                yield from walk(root, None, ())


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


def report_period(args: argparse.Namespace, start: date, end: date, *, audit: bool,
                  include_urls: bool = False) -> dict:
    period_args = argparse.Namespace(**vars(args), since=start.isoformat(), until=end.isoformat(),
                                     query=None, domain=None)
    domains, days, hours = Counter(), Counter(), Counter()
    urls, durations = Counter(), []
    missing = zero = invalid = visits = 0
    for row in history_rows(period_args):
        visits += 1
        urls[row["url"]] += 1
        if row["domain"]:
            domains[row["domain"]] += 1
        if row["visited"]:
            visited = datetime.fromisoformat(row["visited"])
            days[visited.date().isoformat()] += 1
            hours[f"{visited.hour:02d}"] += 1
        if audit:
            duration = row["visit_duration_us"]
            if duration is None:
                missing += 1
            elif not isinstance(duration, int) or duration < 0:
                invalid += 1
            elif duration == 0:
                zero += 1
            else:
                durations.append(duration)
    day_count = (end - start).days + 1
    result = {"start": start.isoformat(), "end": end.isoformat(), "visits": visits,
              "unique_urls": len(urls), "top_domains": [
                  {"domain": name, "visits": count} for name, count in domains.most_common(args.top)],
              "by_day": {(start + timedelta(days=index)).isoformat():
                         days[(start + timedelta(days=index)).isoformat()]
                         for index in range(day_count)},
              "by_hour": {f"{hour:02d}": hours[f"{hour:02d}"] for hour in range(24)}}
    result["_domain_counts"] = domains
    if include_urls:
        result["_url_counts"] = urls
    if audit:
        ordered = sorted(durations)
        result["duration_quality"] = {
            "missing": missing, "invalid": invalid, "zero": zero, "positive": len(ordered),
            "missing_percent": round(missing / visits * 100, 1) if visits else 0.0,
            "invalid_percent": round(invalid / visits * 100, 1) if visits else 0.0,
            "zero_percent": round(zero / visits * 100, 1) if visits else 0.0,
            "positive_percent": round(len(ordered) / visits * 100, 1) if visits else 0.0,
            "median_us": int(statistics.median(ordered)) if ordered else None,
            "p95_us": ordered[(95 * len(ordered) - 1) // 100] if ordered else None,
            "max_us": ordered[-1] if ordered else None,
            "warning": "Visit duration includes inactive time; it is not active browsing time."}
    return result


def comparison_dates(days: int) -> tuple[date, date, date, date]:
    today = date.today()
    start = today - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return today, start, previous_start, previous_end


def report(args: argparse.Namespace) -> dict:
    today, start, previous_start, previous_end = comparison_dates(args.days)
    current = report_period(args, start, today, audit=True)
    previous = report_period(args, previous_start, previous_end, audit=False)
    prior_domains = previous.pop("_domain_counts")
    current.pop("_domain_counts")
    for item in current["top_domains"]:
        item["previous_visits"] = prior_domains.get(item["domain"], 0)
        item["change"] = item["visits"] - item["previous_visits"]
    return {"current": current, "previous": previous,
            "change": {"visits": current["visits"] - previous["visits"],
                       "unique_urls": current["unique_urls"] - previous["unique_urls"]},
            "today_incomplete": True}


def insights(args: argparse.Namespace) -> dict:
    today, start, previous_start, previous_end = comparison_dates(args.days)
    current = report_period(args, start, today, audit=False, include_urls=True)
    previous = report_period(args, previous_start, previous_end, audit=False, include_urls=True)
    current_domains, previous_domains = current["_domain_counts"], previous["_domain_counts"]
    current_urls = current["_url_counts"]
    visits = current["visits"]

    def share(count: int) -> float:
        return round(count / visits * 100, 1) if visits else 0.0

    new = sorted(((domain, count) for domain, count in current_domains.items()
                  if domain not in previous_domains), key=lambda item: (-item[1], item[0]))
    recurring = {domain: count for domain, count in current_domains.items()
                 if domain in previous_domains}
    changes = [{"domain": domain, "current_visits": current_domains[domain],
                "previous_visits": previous_domains[domain],
                "change": current_domains[domain] - previous_domains[domain]}
               for domain in current_domains.keys() | previous_domains.keys()]
    revisited = sorted(((url, count) for url, count in current_urls.items() if count > 1),
                       key=lambda item: (-item[1], item[0]))
    busy_days = sorted(((day, count) for day, count in current["by_day"].items() if count),
                       key=lambda item: (-item[1], item[0]))
    busy_hours = sorted(((hour, count) for hour, count in current["by_hour"].items() if count),
                        key=lambda item: (-item[1], item[0]))
    return {
        "current": {key: current[key] for key in ("start", "end", "visits", "unique_urls")},
        "previous": {key: previous[key] for key in ("start", "end", "visits", "unique_urls")},
        "change": {"visits": visits - previous["visits"],
                   "unique_urls": current["unique_urls"] - previous["unique_urls"]},
        "domains": {
            "new_vs_previous": {"count": len(new), "visits": sum(count for _, count in new),
                                "top": [{"domain": domain, "visits": count}
                                        for domain, count in new[:args.top]]},
            "recurring": {"count": len(recurring), "visits": sum(recurring.values())},
            "largest_increases": sorted((item for item in changes if item["change"] > 0),
                                        key=lambda item: (-item["change"], item["domain"]))[:args.top],
            "largest_decreases": sorted((item for item in changes if item["change"] < 0),
                                        key=lambda item: (item["change"], item["domain"]))[:args.top]},
        "revisits": {"urls_visited_multiple_times": len(revisited),
                     "repeat_visits": sum(count - 1 for _, count in revisited),
                     "top": [{"url": url, "visits": count} for url, count in revisited[:args.top]]},
        "concentration": {
            "active_days": len(busy_days),
            "top_3_days_share_percent": share(sum(count for _, count in busy_days[:3])),
            "top_3_hours_share_percent": share(sum(count for _, count in busy_hours[:3])),
            "top_days": [{"date": day, "visits": count, "share_percent": share(count)}
                         for day, count in busy_days[:args.top]],
            "top_hours": [{"hour": hour, "visits": count, "share_percent": share(count)}
                          for hour, count in busy_hours[:args.top]]},
        "today_incomplete": True,
        "note": "Counts are recorded visits, not active browsing time; available history may be incomplete."}


def search(args: argparse.Namespace) -> list[dict]:
    matches_by_url = {}
    indexed_matches = {}
    sources = (("history", history_rows), ("bookmarks", bookmark_rows),
               ("downloads", download_rows))
    matching_urls = set()
    tabs = []
    if args.include_tabs:
        with closing(tab_rows(argparse.Namespace(**{**vars(args), "query": None}))) as rows:
            tabs = list(rows)
    if args.query:
        for _source, reader in sources:
            with closing(reader(args)) as rows:
                matching_urls.update(row.get("url") or row.get("path") for row in rows)
        matching_urls.update(row["url"] for row in tabs
                             if matches(f"{row['title']} {row['url']}", args.query))
    unfiltered = argparse.Namespace(**{**vars(args), "query": None})
    for source, reader in sources:
        rows = reader(unfiltered) if args.query else reader(args)
        with closing(rows):
            for row in rows:
                url = row.get("url") or row.get("path")
                if not url or (args.query and url not in matching_urls):
                    continue
                match = {"source": source, "profile": row["profile"],
                         "title": row.get("title") or row.get("filename") or "",
                         "timestamp": row.get("visited") or row.get("started") or row.get("added")}
                if source == "bookmarks":
                    match["folder"] = row["folder"]
                if source == "downloads":
                    match["path"] = row["path"]
                entry = matches_by_url.setdefault(url, {"url": row.get("url") or "", "matches": []})
                match_key = (url, source, row["profile"], row.get("id") or row.get("path") or row.get("folder"))
                if source == "history" and match_key in indexed_matches:
                    indexed_matches[match_key]["visits"] += 1
                elif source == "downloads" and match_key in indexed_matches:
                    indexed_matches[match_key]["downloads"] += 1
                elif match_key not in indexed_matches:
                    if source == "history":
                        match["visits"] = 1
                    if source == "downloads":
                        match["downloads"] = 1
                    indexed_matches[match_key] = match
                    entry["matches"].append(match)
    for row in tabs:
        if args.query and row["url"] not in matching_urls:
            continue
        entry = matches_by_url.setdefault(row["url"], {"url": row["url"], "matches": []})
        entry["matches"].append({"source": "tabs", "profile": None,
                                 "title": row["title"], "timestamp": None,
                                 "window": row["window"], "tab": row["tab"]})
    result = list(matches_by_url.values())
    result.sort(key=lambda item: (any(match["source"] == "tabs" for match in item["matches"]),
                                  max((match["timestamp"] or "" for match in item["matches"]), default=""),
                                  item["url"]), reverse=True)
    return result[:args.limit] if args.limit else result


def bridge_status() -> dict:
    config = bridge.load_config()
    response = bridge.request({"op": "ping"})
    if response.get("pairing_code") != config["pairing_code"]:
        raise VivaldiError("Connected extension does not match the paired Vivaldi profile")
    return {"connected": True, "profile": config["profile"], "data_dir": config["data_dir"]}


def bookmark_operation(args: argparse.Namespace) -> dict:
    selected = selected_profiles(args)
    if len(selected) != 1:
        raise VivaldiError("Bookmark changes require exactly one profile")
    config = bridge.load_config()
    if (config["profile"] != selected[0]["id"] or
            config["data_dir"] != str(args.data_dir.resolve())):
        raise VivaldiError("This profile is not paired with the bookmark bridge")
    bridge_status()
    operation = {"kind": args.bookmark_action}
    operation["trash"] = bridge.trash_root_id(args.data_dir, selected[0]["id"])
    if args.bookmark_action in ("move", "edit", "folder-rename"):
        operation["id"] = args.id
    if args.bookmark_action == "move":
        operation["to"] = args.to
    elif args.bookmark_action == "edit":
        if args.title is not None:
            operation["title"] = args.title
        if args.url is not None:
            operation["url"] = args.url
    elif args.bookmark_action == "folder-create":
        operation.update(parent=args.parent, title=args.title)
    elif args.bookmark_action == "folder-rename":
        operation["title"] = args.title
    snapshot = bridge.request({"op": "inspect", "operation": operation}).get("snapshot")
    if not isinstance(snapshot, dict):
        raise bridge.BridgeError("Invalid bookmark preview from the extension")
    expected_token = bridge.token(config, operation, snapshot)
    before = snapshot.get("item")
    after = dict(before or {})
    if args.bookmark_action == "move":
        after["parentId"] = args.to
    elif args.bookmark_action in ("edit", "folder-rename"):
        for field in ("title", "url"):
            if field in operation:
                after[field] = operation[field]
    else:
        after = {"parentId": args.parent, "title": args.title, "url": None}
    if args.apply is None:
        return {"status": "preview", "operation": operation, "before": before,
                "destination": snapshot.get("destination") or snapshot.get("parent"),
                "after": after, "token": expected_token}
    if not secrets.compare_digest(args.apply, expected_token):
        raise VivaldiError("Bookmark state changed or preview token is invalid; preview again")
    result = bridge.request({"op": "execute", "operation": operation,
                             "expected": snapshot, "token": args.apply})
    if not isinstance(result.get("after"), dict):
        raise bridge.BridgeError("Could not verify bookmark result from the extension")
    return {"status": "applied", "operation": operation, "before": before,
            "after": result["after"]}


def show(rows, args: argparse.Namespace, kind: str) -> None:
    if kind in ("bridge", "bookmark-change"):
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif kind == "bridge":
            print(f"Bridge connected: {rows['profile']} ({rows['data_dir']})" if rows.get("connected")
                  else f"Bridge configured: {rows['profile']} ({rows['data_dir']}); restart Vivaldi and check status")
        else:
            print(f"{rows['status'].capitalize()}: {rows['operation']['kind']}")
            print("Before:", json.dumps(rows["before"], ensure_ascii=False))
            if rows.get("destination"):
                print("Destination:", json.dumps(rows["destination"], ensure_ascii=False))
            print("After:", json.dumps(rows["after"], ensure_ascii=False))
            if rows["status"] == "preview":
                print(f"No changes made. Run the same command with --apply {rows['token']}")
        return
    if kind in ("stats", "report", "insights"):
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif kind == "report":
            current, previous = rows["current"], rows["previous"]
            print(f"Visits: {current['visits']} ({rows['change']['visits']:+} vs previous {args.days} days)")
            print(f"Unique URLs: {current['unique_urls']} ({rows['change']['unique_urls']:+})")
            print("Today is incomplete. Visit duration is not active browsing time.")
            for item in current["top_domains"]:
                print(f"{item['visits']:>7}  {item['domain']} ({item['change']:+})")
            print("By day:", json.dumps(current["by_day"], ensure_ascii=False))
            print("By hour:", json.dumps(current["by_hour"], ensure_ascii=False))
            print("Duration quality:", json.dumps(current["duration_quality"], ensure_ascii=False))
        elif kind == "insights":
            current, previous = rows["current"], rows["previous"]
            print(f"Period: {current['start']} to {current['end']} "
                  f"vs {previous['start']} to {previous['end']}")
            print(f"Recorded visits: {current['visits']} ({rows['change']['visits']:+} vs previous {args.days} days)")
            print(f"Distinct URLs: {current['unique_urls']} ({rows['change']['unique_urls']:+})")
            new, recurring = rows["domains"]["new_vs_previous"], rows["domains"]["recurring"]
            print(f"Domains absent from previous period: {new['count']} ({new['visits']} visits)")
            print(f"Recurring domains: {recurring['count']} ({recurring['visits']} visits)")
            for item in new["top"]:
                print(f"  New: {item['domain']} ({item['visits']} visits)")
            print("Largest domain increases:")
            for item in rows["domains"]["largest_increases"]:
                print(f"  {item['domain']}: {item['current_visits']} ({item['change']:+})")
            print("Largest domain decreases:")
            for item in rows["domains"]["largest_decreases"]:
                print(f"  {item['domain']}: {item['current_visits']} ({item['change']:+})")
            revisits = rows["revisits"]
            print(f"Revisited URLs: {revisits['urls_visited_multiple_times']} "
                  f"({revisits['repeat_visits']} visits after the first)")
            for item in revisits["top"]:
                print(f"  {item['visits']} visits  {item['url']}")
            concentration = rows["concentration"]
            print(f"Active days: {concentration['active_days']}; "
                  f"top 3 days: {concentration['top_3_days_share_percent']}% of visits; "
                  f"top 3 hours: {concentration['top_3_hours_share_percent']}% of visits")
            for item in concentration["top_days"][:3]:
                print(f"  Day: {item['date']} ({item['visits']} visits)")
            for item in concentration["top_hours"][:3]:
                print(f"  Hour: {item['hour']}:00 ({item['visits']} visits)")
            print("Today is incomplete. Counts are visits, not active browsing time.")
        else:
            print(f"Visits: {rows['visits']} | Unique URLs: {rows['unique_urls']}")
            for item in rows["top_domains"]:
                print(f"{item['visits']:>7}  {item['domain']}")
        return
    if kind == "search":
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for row in rows:
                sources = ",".join(dict.fromkeys(match["source"] for match in row["matches"]))
                title = next((match["title"] for match in row["matches"] if match["title"]), "")
                location = row["url"] or next((match.get("path", "") for match in row["matches"]
                                                if match.get("path")), "")
                profile = ""
                if args.all_profiles:
                    profile = ",".join(dict.fromkeys(
                        match["profile"] or "unattributed" for match in row["matches"])) + "\t"
                print(f"{profile}{sources}\t{title}\t{location}")
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
            profile = f"{row['profile']}\t" if getattr(args, "all_profiles", False) else ""
            if kind == "profiles":
                print(f"{row['id']}\t{row['name']}")
            elif kind == "tabs":
                print(f"{row['window']}:{row['tab']}\t{row['title']}\t{row['url']}")
            elif kind == "downloads":
                print(f"{profile}{row['started']}\t{row['filename']}\t{row['url']}")
            elif kind == "folders":
                print(f"{profile}{row['id']}\t{row['path']}")
            else:
                stamp = row.get("visited") or row.get("added") or ""
                if kind == "bookmarks":
                    print(f"{profile}{row['id']}\t{row['folder']}\t{row['title']}\t{row['url']}")
                else:
                    print(f"{profile}{stamp}\t{row['title']}\t{row['url']}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="vivaldi", description="Read-only local access to Vivaldi data with opt-in bookmark changes")
    root.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = root.add_subparsers(dest="command", required=True)

    def common(command, *, profile=True, dates=False, domain=False, folder=False,
               query=False, query_help="terms in title or URL", limit=True):
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                             help="Vivaldi data directory (default: macOS profile directory)")
        command.add_argument("--json", action="store_true", help="JSON output for scripts")
        if profile:
            selected = command.add_mutually_exclusive_group()
            selected.add_argument("--profile", default=None,
                                  help="profile ID or name (default: Default)")
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
    report_parser = sub.add_parser("report", help="compare recent browsing periods")
    common(report_parser, limit=False)
    report_parser.add_argument("--days", type=int, default=30, help="calendar days per period (default: 30)")
    report_parser.add_argument("--top", type=int, default=10, help="number of top domains")
    insights_parser = sub.add_parser("insights", help="find visit trends and revisited pages")
    common(insights_parser, limit=False)
    insights_parser.add_argument("--days", type=int, default=30,
                                 help="calendar days per period (default: 30)")
    insights_parser.add_argument("--top", type=int, default=10, help="maximum items per ranked list")
    search_parser = sub.add_parser("search", help="search across local Vivaldi data")
    common(search_parser)
    search_parser.add_argument("query", help="terms in title, URL, folder, or download path")
    search_parser.add_argument("--include-tabs", action="store_true",
                               help="include open tabs (profile cannot be identified)")
    search_parser.set_defaults(since=None, until=None, domain=None, folder=None)
    stats_parser = sub.add_parser("stats", help="statistics for available history")
    common(stats_parser, dates=True, domain=True, limit=False)
    stats_parser.add_argument("--top", type=int, default=10, help="number of top domains")
    bookmark_parser = sub.add_parser("bookmark", help="inspect or organize individual bookmarks")
    bookmark_sub = bookmark_parser.add_subparsers(dest="bookmark_action", required=True)
    folders_parser = bookmark_sub.add_parser("folders", help="list bookmark folders and IDs")
    common(folders_parser)
    for action in ("move", "edit", "folder-create", "folder-rename"):
        action_parser = bookmark_sub.add_parser(action, help=f"preview or apply bookmark {action}")
        common(action_parser, limit=False)
        action_parser.add_argument("--apply", metavar="TOKEN", help="apply a matching preview")
        if action != "folder-create":
            action_parser.add_argument("id", help="bookmark or folder ID")
        if action == "move":
            action_parser.add_argument("--to", required=True, help="destination folder ID")
        if action == "folder-create":
            action_parser.add_argument("--parent", required=True, help="parent folder ID")
        if action in ("edit", "folder-create", "folder-rename"):
            action_parser.add_argument("--title", required=action != "edit", help="new title")
        if action == "edit":
            action_parser.add_argument("--url", help="new bookmark URL")
    bridge_parser = sub.add_parser("bridge", help="configure the local bookmark extension bridge")
    bridge_sub = bridge_parser.add_subparsers(dest="bridge_action", required=True)
    setup_parser = bridge_sub.add_parser("setup", help="pair one local Vivaldi profile")
    common(setup_parser, limit=False)
    setup_parser.add_argument("--extension-id", required=True, help="ID shown on vivaldi://extensions")
    setup_parser.add_argument("--pair", required=True, help="code shown in the extension options")
    status_parser = bridge_sub.add_parser("status", help="check the paired extension")
    status_parser.add_argument("--json", action="store_true", help="JSON output for scripts")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if hasattr(args, "limit") and args.limit < 0:
        raise SystemExit("--limit cannot be negative")
    if hasattr(args, "top") and args.top < 0:
        raise SystemExit("--top cannot be negative")
    if hasattr(args, "days") and not 1 <= args.days <= 3650:
        raise SystemExit("--days must be between 1 and 3650")
    try:
        if (hasattr(args, "profile") and args.profile is None and
                not args.all_profiles):
            args.profile = "Default"
            if len(profiles(args.data_dir)) > 1:
                print("Using profile Default; pass --profile or --all-profiles to change scope",
                      file=sys.stderr)
        if args.command == "bridge":
            if args.bridge_action == "setup":
                chosen = selected_profiles(args)
                if len(chosen) != 1:
                    raise VivaldiError("Bridge setup requires one profile")
                result = bridge.setup(args.data_dir, chosen[0]["id"], args.extension_id,
                                      args.pair, Path(__file__).with_name("bridge_host.py"))
                result["connected"] = False
            else:
                result = bridge_status()
            show(result, args, "bridge")
            return 0
        if args.command == "bookmark":
            if args.bookmark_action == "folders":
                show(folder_rows(args), args, "folders")
            else:
                show(bookmark_operation(args), args, "bookmark-change")
            return 0
        commands = {"profiles": lambda: profiles(args.data_dir),
                    "history": lambda: history_rows(args),
                    "bookmarks": lambda: bookmark_rows(args),
                    "downloads": lambda: download_rows(args),
                    "tabs": lambda: tab_rows(args),
                    "stats": lambda: stats(args),
                    "report": lambda: report(args),
                    "insights": lambda: insights(args),
                    "search": lambda: search(args)}
        show(commands[args.command](), args, args.command)
        return 0
    except (VivaldiError, bridge.BridgeError, OSError, sqlite3.Error) as exc:
        print(f"vivaldi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
