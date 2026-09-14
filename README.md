# Vivaldi CLI

Local access to Vivaldi data on macOS. Existing commands remain read-only and need no extension, network service, or third-party Python package. An experimental, opt-in bookmark bridge is available from source only. This is an independent project, not affiliated with Vivaldi Technologies. [MIT licensed](LICENSE).

## Install

Install from the [Homebrew tap](https://github.com/matheuscoelhomalta/homebrew-vivaldi-cli):

```sh
brew install matheuscoelhomalta/vivaldi-cli/vivaldi-cli
vivaldi --version
```

Homebrew installs the required Python. Alternatively, with Python 3.10 or newer, run `python3 vivaldi.py <command>` from this repository. The examples below use the Homebrew-installed `vivaldi` command.

The report, insights, unified search, and bookmark bridge below are an **unreleased local pilot**. Run them as `python3 vivaldi.py ...` from this checkout; they are not in the current Homebrew release.

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

In the local pilot:

```sh
python3 vivaldi.py report --days 30 --all-profiles --json
python3 vivaldi.py insights --days 30 --all-profiles --json
python3 vivaldi.py search "example" --all-profiles --json
python3 vivaldi.py search "example" --include-tabs --json
```

`report` includes today (which is incomplete), compares it with the preceding equal-length period, and audits the availability and distribution of Chromium's visit-duration field. It does **not** sum duration into time spent: a visit can include inactive time. `insights` compares those same periods to show domains absent from the previous period, recurring domains, the largest visit-count changes, exactly matching URLs visited more than once, and how visits concentrate across days and hours. "New" means absent from the immediately preceding period, not never visited before. A visit's hour is not time spent in that hour; today's counts are incomplete. `search` groups exact duplicate URLs across history, bookmarks, and downloads; if a query matches one record, its exact-URL group includes other records even if their titles or folders do not match. Repeated visits and downloads expose their respective counts. Tabs are opt-in because they cannot be attributed to a profile. Bookmark rows now include IDs; `python3 vivaldi.py bookmark folders --json` lists folder IDs.

Every command supports `--json`. List commands support `--limit` (default: 50; `0` means all). History, downloads, and stats support inclusive local-date filters `--since` and `--until`. History, bookmarks, downloads, tabs, and stats support an exact `--domain` filter. Bookmarks also support `--folder`: match a full folder path, case-insensitively, including its subfolders. Without `--all-profiles`, the CLI uses `Default` or the profile selected with `--profile`; when multiple profiles exist, it announces an implicit `Default` selection on stderr. With `--all-profiles`, history and downloads are merged by date before `--limit` is applied, and text results identify their profiles. Use `--data-dir` for a different local Vivaldi data directory. Run `vivaldi <command> --help` for the full options.

History and downloads are read from a temporary SQLite snapshot. The CLI uses SQLite's backup API when available. If an exclusive lock prevents that, it copies the database and any journal files, then accepts the snapshot only if the source remained stable and SQLite's integrity check passes; otherwise it asks you to retry. The original profile is never opened for writing. Tabs use macOS Apple Events: Vivaldi must be open, and macOS may ask you to allow the app running the CLI to control Vivaldi. Tab results cover windows accessible to AppleScript and do not identify a profile for each tab.

The CLI has no persistent history cache and cannot read passwords or cookies. Stats count recorded visits, not time spent. Recent data may be absent until Vivaldi writes it to disk; available history depends on local retention and sync.

## Experimental bookmark bridge (local pilot)

The bridge is only for organizing individual bookmarks: move a bookmark, edit its title/URL, or create/rename a folder. It does not delete anything or move whole folders. The CLI never writes the `Bookmarks` profile file. Vivaldi must be open, and the bridge uses a locally installed extension with the `bookmarks`, `nativeMessaging`, and `storage` permissions. Native Messaging uses a private Unix socket, not a network server. If Vivaldi Sync is enabled, applied changes may propagate to your other devices.

Test with a **disposable Vivaldi profile**, not your usual profile. Open Vivaldi with a separate user-data directory, enable Developer mode at `vivaldi://extensions`, and load the `extension/` directory unpacked. Copy its extension ID and open its Options page for the pairing code. In this checkout, run:

```sh
python3 vivaldi.py bridge setup --data-dir /path/to/test-profile --profile Default --extension-id EXTENSION_ID --pair PAIRING_CODE
```

Setup registers a Native Messaging host in the selected user-data directory's `NativeMessagingHosts/` folder (the default is `~/Library/Application Support/Vivaldi/NativeMessagingHosts/`) and stores a private pairing configuration in `~/Library/Application Support/vivaldi-cli/`. Reload the extension or restart the disposable Vivaldi instance, then check `python3 vivaldi.py bridge status`. Only the paired profile may be changed. The host needs the Python interpreter and checkout paths used during setup to remain available; rerun setup if either path changes.

Find an individual bookmark ID with `python3 vivaldi.py bookmarks --json` and folder IDs with `python3 vivaldi.py bookmark folders --json`. A command without `--apply` is a preview and prints a token. Run the **same command** again with `--apply TOKEN` to commit, for example:

```sh
python3 vivaldi.py bookmark move 10 --to 3 --data-dir /path/to/test-profile
python3 vivaldi.py bookmark move 10 --to 3 --data-dir /path/to/test-profile --apply TOKEN
python3 vivaldi.py bookmark edit 10 --title "New title" --data-dir /path/to/test-profile
python3 vivaldi.py bookmark folder-create --parent 3 --title "New folder" --data-dir /path/to/test-profile
python3 vivaldi.py bookmark folder-rename 3 --title "Renamed" --data-dir /path/to/test-profile
```

The token is tied to the item's current state. A changed or ambiguous item is rejected; preview it again. This pilot has not been packaged or released. Remove the exact host manifest and private bridge directory after testing if you do not want to keep the local integration; neither is part of the Vivaldi profile.

## Troubleshooting

- If Homebrew reports incompatible Apple Command Line Tools, update them through macOS Software Update or [Apple Developer Downloads](https://developer.apple.com/download/all/). Homebrew does not update Apple's tools.
- If `vivaldi profiles` cannot find data, open Vivaldi once and check its profile directory, or pass `--data-dir`.
- If `vivaldi tabs` fails, open Vivaldi and allow Apple Events access in macOS Privacy & Security → Automation for the app running the command.
- If history is locked or changes during a snapshot, retry when Vivaldi is idle.

## Development

Run synthetic tests without accessing personal profiles:

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_extension.mjs
```

CI runs Python tests on macOS with Python 3.10–3.14 and extension tests with Node 22. It does not access real Vivaldi profiles. The local pilot was also checked on Vivaldi 8.2.4133.52 in an unsynced disposable macOS profile: folder creation/rename and bookmark move/edit succeeded through `chrome.bookmarks`, duplicate creation and a stale token were rejected, and the resulting bookmark and folder remained visible to the extension API and on disk after restarting that Vivaldi instance. This is not a cross-version or cross-platform validation; no personal bookmarks were changed.
