"""Local-only bridge between the CLI and Vivaldi's bookmarks extension."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import struct
import sys


HOST_NAME = "com.vivaldi_cli.bookmarks"
EXTENSION_ID = re.compile(r"[a-p]{32}\Z")
MAX_MESSAGE = 1024 * 1024


class BridgeError(Exception):
    pass


def bridge_dir() -> Path:
    override = os.environ.get("VIVALDI_CLI_BRIDGE_DIR")
    return Path(override) if override else Path.home() / "Library/Application Support/vivaldi-cli"


def config_path() -> Path:
    return bridge_dir() / "bridge.json"


def socket_path() -> Path:
    return bridge_dir() / "bridge.sock"


def manifest_path(data_dir: Path | None = None) -> Path:
    override = os.environ.get("VIVALDI_CLI_MANIFEST_DIR")
    base = (Path(override) if override else
            (data_dir if data_dir is not None else
             Path.home() / "Library/Application Support/Vivaldi") / "NativeMessagingHosts")
    return base / f"{HOST_NAME}.json"


def load_config() -> dict:
    try:
        config = json.loads(config_path().read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not all(
                isinstance(config.get(key), str) and config[key]
                for key in ("data_dir", "profile", "extension_id", "pairing_code", "secret")):
            raise ValueError("incomplete bridge configuration")
        bytes.fromhex(config["secret"])
        return config
    except (OSError, ValueError, TypeError) as exc:
        raise BridgeError("Bridge is not configured; run 'vivaldi bridge setup' first") from exc


def setup(data_dir: Path, profile: str, extension_id: str, pairing_code: str, host_path: Path) -> dict:
    if not EXTENSION_ID.fullmatch(extension_id):
        raise BridgeError("Extension ID must be 32 lowercase letters from a to p")
    if not re.fullmatch(r"[0-9a-f]{32}", pairing_code):
        raise BridgeError("Pairing code must be 32 lowercase hexadecimal characters")
    directory = bridge_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require_private_directory(directory)
    config = {"data_dir": str(data_dir.resolve()), "profile": profile,
              "extension_id": extension_id, "pairing_code": pairing_code,
              "secret": secrets.token_hex(32)}
    write_private_json(config_path(), config)
    launcher = directory / "bridge-host"
    write_private_file(launcher, f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                       f"{shlex.quote(str(host_path.resolve()))} \"$@\"\n", 0o700)
    manifest = {"name": HOST_NAME, "description": "Vivaldi CLI bookmark bridge",
                "path": str(launcher), "type": "stdio",
                "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    path = manifest_path(data_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private_json(path, manifest)
    return {"profile": profile, "data_dir": config["data_dir"], "manifest": str(path)}


def write_private_json(path: Path, value: dict) -> None:
    write_private_file(path, json.dumps(value, indent=2) + "\n", 0o600)


def write_private_file(path: Path, content: str, mode: int) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def require_private_directory(directory: Path) -> None:
    state = directory.lstat()
    if (not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid() or
            state.st_mode & 0o077):
        raise BridgeError("Bridge directory must be owned by you and accessible only to you")


def request(message: dict, *, timeout: float = 15) -> dict:
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(payload) > MAX_MESSAGE:
                raise BridgeError("Bridge request is too large")
            connection.sendall(payload)
            response = bytearray()
            while b"\n" not in response:
                chunk = connection.recv(65536)
                if not chunk:
                    raise BridgeError("Bridge closed before replying")
                response.extend(chunk)
                if len(response) > MAX_MESSAGE:
                    raise BridgeError("Bridge reply is too large")
        result = json.loads(response.split(b"\n", 1)[0])
    except (OSError, ValueError) as exc:
        raise BridgeError("Bridge is unavailable; open Vivaldi and check 'vivaldi bridge status'") from exc
    if not isinstance(result, dict):
        raise BridgeError("Invalid bridge reply")
    if not result.get("ok"):
        raise BridgeError(str(result.get("error") or "Bookmark operation failed"))
    return result


def token(config: dict, operation: dict, snapshot: dict) -> str:
    payload = json.dumps({"operation": operation, "snapshot": snapshot}, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hmac.new(bytes.fromhex(config["secret"]), payload, hashlib.sha256).hexdigest()


def trash_root_id(data_dir: Path, profile: str) -> str:
    try:
        root = json.loads((data_dir / profile / "Bookmarks").read_text(encoding="utf-8"))["roots"]["trash"]
        if not isinstance(root, dict) or root.get("id") is None:
            raise ValueError("missing trash root ID")
        return str(root["id"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BridgeError(f"Could not inspect bookmark trash for profile: {profile}") from exc


def authorized_message(config: dict, message: dict) -> dict:
    if not isinstance(message, dict):
        raise BridgeError("Invalid bridge request")
    if message.get("op") == "ping":
        return {"op": "ping"}
    if message.get("op") not in ("inspect", "execute"):
        raise BridgeError("Unsupported bridge request")
    operation = message.get("operation")
    if not isinstance(operation, dict) or operation.get("trash") != trash_root_id(
            Path(config["data_dir"]), config["profile"]):
        raise BridgeError("Bookmark trash guard is missing or invalid")
    if message["op"] == "inspect":
        return {"op": "inspect", "operation": operation}
    expected, supplied = message.get("expected"), message.get("token")
    if (not isinstance(expected, dict) or not isinstance(supplied, str) or
            not hmac.compare_digest(supplied, token(config, operation, expected))):
        raise BridgeError("A matching preview token is required for bookmark changes")
    return {"op": "execute", "operation": operation, "expected": expected}


def read_exact(handle, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = handle.read(count - len(result))
        if not chunk:
            raise EOFError("Native messaging port closed")
        result.extend(chunk)
    return bytes(result)


def native_exchange(message: dict) -> dict:
    encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_MESSAGE:
        raise BridgeError("Extension request is too large")
    sys.stdout.buffer.write(struct.pack("=I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
    length = struct.unpack("=I", read_exact(sys.stdin.buffer, 4))[0]
    if length > MAX_MESSAGE:
        raise BridgeError("Extension reply is too large")
    reply = json.loads(read_exact(sys.stdin.buffer, length))
    if not isinstance(reply, dict):
        raise BridgeError("Invalid extension reply")
    return reply


def host_main(origin: str) -> int:
    config = load_config()
    if origin != f"chrome-extension://{config['extension_id']}/":
        raise BridgeError("Unexpected extension origin")
    directory = bridge_dir()
    require_private_directory(directory)
    path = socket_path()
    lock_fd = os.open(directory / "bridge.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "rb") as lock:
        state = os.fstat(lock.fileno())
        if not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid() or state.st_mode & 0o077:
            raise BridgeError("Bridge lock must be a private regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BridgeError("Another bridge is already connected") from exc
        if path.exists() or path.is_symlink():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise BridgeError("Bridge socket path is occupied by a non-socket file")
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.3)
                    probe.connect(str(path))
                raise BridgeError("Another bridge is already connected")
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
        owned_inode = None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                owned_inode = path.lstat().st_ino
                os.chmod(path, 0o600)
                listener.listen(1)
                while True:
                    with listener.accept()[0] as connection:
                        connection.settimeout(20)
                        payload = bytearray()
                        while b"\n" not in payload:
                            chunk = connection.recv(65536)
                            if not chunk:
                                break
                            payload.extend(chunk)
                            if len(payload) > MAX_MESSAGE:
                                raise BridgeError("Bridge request is too large")
                        if not payload:
                            continue
                        try:
                            outbound = authorized_message(config, json.loads(payload.split(b"\n", 1)[0]))
                            if outbound["op"] == "execute":
                                paired = native_exchange({"op": "ping"})
                                if not paired.get("ok") or paired.get("pairing_code") != config["pairing_code"]:
                                    raise BridgeError("Connected extension does not match the paired profile")
                            reply = native_exchange(outbound)
                        except (BridgeError, ValueError, TypeError) as exc:
                            reply = {"ok": False, "error": str(exc)}
                        connection.sendall(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
        except EOFError:
            return 0
        finally:
            try:
                if owned_inode is not None and path.lstat().st_ino == owned_inode:
                    path.unlink()
            except FileNotFoundError:
                pass


def main_native(origin: str) -> int:
    try:
        return host_main(origin)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"vivaldi bridge: {exc}", file=sys.stderr)
        return 1
