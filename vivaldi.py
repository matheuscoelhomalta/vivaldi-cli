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
from urllib.parse import urlsplit


CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
DEFAULT_DATA_DIR = Path.home() / "Library/Application Support/Vivaldi"
VERSION = "0.1.0"


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
        raise VivaldiError(f"Perfil do Vivaldi não encontrado: {data_dir}")
    try:
        cache = json.loads(state.read_text(encoding="utf-8"))["profile"]["info_cache"]
        if not isinstance(cache, dict) or any(not isinstance(item, dict) for item in cache.values()):
            raise ValueError("invalid profile cache")
        return [{"id": key, "name": value.get("name", key)} for key, value in cache.items()]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VivaldiError("Não foi possível ler os perfis em Local State") from exc


def selected_profiles(args: argparse.Namespace) -> list[dict[str, str]]:
    available = profiles(args.data_dir)
    if args.all_profiles:
        return available
    for item in available:
        if args.profile in (item["id"], item["name"]):
            return [item]
    names = ", ".join(item["id"] for item in available)
    raise VivaldiError(f"Perfil '{args.profile}' não encontrado. IDs disponíveis: {names}")


@contextmanager
def history_connection(profile_dir: Path):
    source = profile_dir / "History"
    if not source.is_file():
        raise VivaldiError(f"Histórico não encontrado no perfil: {profile_dir.name}")
    # Vivaldi can hold an exclusive SQLite lock. Never connect for writes to its profile.
    with TemporaryDirectory(prefix="vivaldi-cli-") as temporary:
        copy = Path(temporary) / "History"
        shutil.copyfile(source, copy)
        wal = source.with_name("History-wal")
        if wal.exists():
            shutil.copyfile(wal, copy.with_name("History-wal"))
            uri = copy.as_uri() + "?mode=ro"
        else:
            uri = copy.as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()


def date_bounds(args: argparse.Namespace) -> tuple[int | None, int | None]:
    def to_chromium(day: date) -> int:
        local_midnight = datetime.combine(day, time.min).astimezone(timezone.utc)
        return int((local_midnight - CHROMIUM_EPOCH).total_seconds() * 1_000_000)

    try:
        start = date.fromisoformat(args.since) if args.since else None
        end = date.fromisoformat(args.until) if args.until else None
    except ValueError as exc:
        raise VivaldiError("Datas devem estar no formato AAAA-MM-DD") from exc
    if start and end and start > end:
        raise VivaldiError("--since deve ser anterior ou igual a --until")
    try:
        return to_chromium(start) if start else None, to_chromium(end + timedelta(days=1)) if end else None
    except OverflowError as exc:
        raise VivaldiError("Data fora do intervalo suportado") from exc


def matches(value: str, query: str | None) -> bool:
    return not query or all(term.casefold() in value.casefold() for term in query.split())


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
                if args.domain and domain != args.domain.lower().removeprefix("www."):
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
            raise VivaldiError(f"Favoritos não encontrados no perfil: {profile['id']}")
        try:
            roots = json.loads(path.read_text(encoding="utf-8"))["roots"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise VivaldiError(f"Não foi possível ler favoritos do perfil: {profile['id']}") from exc

        def walk(node: dict, folder: tuple[str, ...]):
            if node.get("type") == "folder":
                next_folder = folder + ((node.get("name") or ""),)
                for child in node.get("children", []):
                    yield from walk(child, next_folder)
            elif node.get("type") == "url":
                url = node.get("url", "")
                title = node.get("name", "")
                folder_name = "/".join(part for part in folder if part)
                if matches(f"{title} {url} {folder_name}", args.query):
                    yield {"profile": profile["id"], "title": title, "url": url,
                           "domain": domain_of(url), "folder": folder_name,
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
                if args.domain and domain_of(url) != args.domain.lower().removeprefix("www."):
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
        raise VivaldiError("A consulta de abas requer macOS e Vivaldi aberto")
    try:
        output = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA_TABS],
                                capture_output=True, text=True, check=True, timeout=25)
        rows = json.loads(output.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise VivaldiError("Não foi possível consultar as abas; verifique se o Vivaldi está aberto e permita Apple Events") from exc
    for row in rows:
        if matches(f"{row['title']} {row['url']}", args.query):
            row["domain"] = domain_of(row["url"])
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
            print(f"Visitas: {rows['visits']} | URLs distintas: {rows['unique_urls']}")
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
    root = argparse.ArgumentParser(prog="vivaldi", description="Consulta local e somente leitura ao Vivaldi")
    root.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = root.add_subparsers(dest="command", required=True)

    def common(command, *, profile=True, dates=False, domain=False, query=False, limit=True):
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                             help="pasta de dados do Vivaldi (padrão: perfil macOS)")
        command.add_argument("--json", action="store_true", help="saída JSON para scripts")
        if profile:
            selected = command.add_mutually_exclusive_group()
            selected.add_argument("--profile", default="Default", help="ID ou nome do perfil")
            selected.add_argument("--all-profiles", action="store_true", help="todos os perfis locais")
        if query:
            command.add_argument("query", nargs="?", help="termos em título, URL ou pasta")
        if dates:
            command.add_argument("--since", help="data local inicial, AAAA-MM-DD")
            command.add_argument("--until", help="data local final, AAAA-MM-DD (inclusiva)")
        if domain:
            command.add_argument("--domain", help="domínio exato, sem www")
        if limit:
            command.add_argument("--limit", type=int, default=50, help="máximo de resultados; 0 = todos")

    common(sub.add_parser("profiles", help="listar perfis"), profile=False, limit=False)
    common(sub.add_parser("history", help="buscar visitas"), dates=True, domain=True, query=True)
    common(sub.add_parser("bookmarks", help="buscar favoritos"), query=True)
    common(sub.add_parser("downloads", help="buscar downloads"), dates=True, domain=True, query=True)
    common(sub.add_parser("tabs", help="listar abas abertas"), profile=False, query=True)
    stats_parser = sub.add_parser("stats", help="estatísticas do histórico disponível")
    common(stats_parser, dates=True, domain=True, limit=False)
    stats_parser.add_argument("--top", type=int, default=10, help="número de domínios no ranking")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if hasattr(args, "limit") and args.limit < 0:
        raise SystemExit("--limit não pode ser negativo")
    if hasattr(args, "top") and args.top < 0:
        raise SystemExit("--top não pode ser negativo")
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
