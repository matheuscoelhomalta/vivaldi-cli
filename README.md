# Vivaldi CLI

Read-only, local access to Vivaldi data on macOS. No Raycast, browser extension, network service, or third-party Python package is required. This is an independent project, not affiliated with Vivaldi Technologies. [MIT licensed](LICENSE).

## Install

Install from the [Homebrew tap](https://github.com/matheuscoelhomalta/homebrew-vivaldi-cli):

```sh
brew install matheuscoelhomalta/vivaldi-cli/vivaldi-cli
vivaldi --version
```

Homebrew installs the required Python. Alternatively, with Python 3.10 or newer, run `python3 vivaldi.py <command>` from this repository. The examples below use the Homebrew-installed `vivaldi` command.

To update or remove the Homebrew installation:

```sh
brew update
brew upgrade matheuscoelhomalta/vivaldi-cli/vivaldi-cli
brew uninstall matheuscoelhomalta/vivaldi-cli/vivaldi-cli
```

Run only the command you need. `brew uninstall` removes the CLI, not your Vivaldi profiles.

## Commands

```sh
vivaldi profiles
vivaldi history "term" --profile Default --since 2026-01-01 --domain example.com
vivaldi bookmarks "term" --all-profiles --domain example.com
vivaldi bookmarks --folder "Bookmarks Bar/Projects" --limit 0
vivaldi downloads "pdf" --since 2026-01-01
vivaldi tabs "term" --domain example.com
vivaldi stats --all-profiles --top 20 --json
```

Every command supports `--json`. List commands support `--limit` (default: 50; `0` means all). History, downloads, and stats support inclusive local-date filters `--since` and `--until`. History, bookmarks, downloads, tabs, and stats support an exact `--domain` filter. Bookmarks also support `--folder`: match a full folder path, case-insensitively, including its subfolders. Without `--all-profiles`, the CLI uses `Default` or the profile selected with `--profile`. With `--all-profiles`, history and downloads are merged by date before `--limit` is applied. Use `--data-dir` for a different local Vivaldi data directory. Run `vivaldi <command> --help` for the full options.

History and downloads are read from a temporary SQLite snapshot. The CLI uses SQLite's backup API when available. If an exclusive lock prevents that, it copies the database and any journal files, then accepts the snapshot only if the source remained stable and SQLite's integrity check passes; otherwise it asks you to retry. The original profile is never opened for writing. Tabs use macOS Apple Events: Vivaldi must be open, and macOS may ask you to allow the app running the CLI to control Vivaldi. Tab results cover windows accessible to AppleScript and do not identify a profile for each tab.

The CLI has no persistent cache and cannot read passwords or cookies or edit browser data. Stats count recorded visits, not time spent. Recent data may be absent until Vivaldi writes it to disk; available history depends on local retention and sync.

## Troubleshooting

- If Homebrew reports incompatible Apple Command Line Tools, update them through macOS Software Update or [Apple Developer Downloads](https://developer.apple.com/download/all/). Homebrew does not update Apple's tools.
- If `vivaldi profiles` cannot find data, open Vivaldi once and check its profile directory, or pass `--data-dir`.
- If `vivaldi tabs` fails, open Vivaldi and allow Apple Events access in macOS Privacy & Security → Automation for the app running the command.
- If history is locked or changes during a snapshot, retry when Vivaldi is idle.

## Development

Run synthetic tests without accessing personal profiles:

```sh
python3 -m unittest discover -s tests -v
```

CI runs these tests on macOS with Python 3.10–3.14. It does not access real Vivaldi profiles.
