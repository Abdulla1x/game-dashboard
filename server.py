"""Local HTTP server for the dashboard.

Binds 127.0.0.1 only and has no authentication — it can start arbitrary executables,
so it must never be reachable from the network.

Copyright (C) 2026 Mohammad Abdullah

This program is free software: you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version. It is distributed
WITHOUT ANY WARRANTY; see the GNU General Public License for more details.
"""

import json
import mimetypes
import os
import posixpath
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import art
import config
import detect
import exefind
import launch as launcher
import playtime
import scan
import winpath
from scanners import shortcuts as shortcut_scanner

STATE = {
    "library": {"games": [], "generated": 0},
    "cfg": config.load(),
    "tracker": None,
    "scanning": False,
    "sizing": False,
    # The port actually bound, which --port can move off the configured one. The
    # request guard compares the Host header against it.
    "port": None,
}
_lock = threading.Lock()


# -- library state -------------------------------------------------------


def load_library(rescan=False):
    with _lock:
        if STATE["scanning"]:
            return STATE["library"]
        STATE["scanning"] = True
    try:
        STATE["cfg"] = config.load()
        if rescan or not os.path.exists(config.LIBRARY_JSON):
            payload = scan.run()
        else:
            payload = config.read_json(config.LIBRARY_JSON, {"games": []})
        STATE["library"] = payload
        return payload
    finally:
        with _lock:
            STATE["scanning"] = False


def find_game(game_id):
    for game in STATE["library"].get("games", []):
        if game["id"] == game_id:
            return game
    return None


def games_payload():
    tracker = STATE["tracker"]
    active = tracker.active_ids() if tracker else set()
    stats = tracker.stats() if tracker else {}

    out = []
    for game in STATE["library"].get("games", []):
        entry = stats.get(game["id"]) or {}
        tracked_total = entry.get("total_seconds", 0)
        tracked_last = entry.get("last_played")
        art_kind, art_version = art.cache_state(game)
        out.append({
            **game,
            "running": game["id"] in active,
            "art_kind": art_kind,
            "art_version": art_version,
            # Steam reports lifetime playtime for owned games; our own tracker only
            # knows what it has watched, so prefer it and fall back to Steam's number.
            "playtime_seconds": tracked_total or (game.get("playtime_minutes") or 0) * 60,
            # Steam knows when *it* last ran a game; our own tracking is more recent
            # when it exists, so prefer it and fall back to Steam's record.
            "last_played": tracked_last or game.get("last_played"),
        })
    return {
        "generated": STATE["library"].get("generated", 0),
        "scanning": STATE["scanning"],
        "sizing": STATE["sizing"],
        "games": out,
    }


# -- folder sizes --------------------------------------------------------


def _dir_size(win_dir):
    total = 0
    root = winpath.native(win_dir)
    for cur, _dirs, files in os.walk(root):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(cur, fname))
            except OSError:
                pass
    return total


def compute_sizes():
    """Walk folder games to fill in size on disk. Slow, so it runs in the background."""
    with _lock:
        if STATE["sizing"]:
            return
        STATE["sizing"] = True
    try:
        path = os.path.join(config.DATA_DIR, "sizes.json")
        sizes = config.read_json(path, {})
        targets = [g for g in STATE["library"].get("games", [])
                   if g.get("install_dir") and not g.get("size_bytes")]

        for i, game in enumerate(targets, 1):
            install = game["install_dir"]
            try:
                total = _dir_size(install)
            except OSError:
                continue
            sizes[install] = total
            game["size_bytes"] = total
            if i % 5 == 0 or i == len(targets):
                config.write_json(path, sizes)
                print(f"[sizes] {i}/{len(targets)}")

        config.write_json(path, sizes)
        config.write_json(config.LIBRARY_JSON, STATE["library"])
    finally:
        with _lock:
            STATE["sizing"] = False


# -- request validation --------------------------------------------------

# Cover art may live anywhere on disk — the user picks the file — so the constraint is
# on *what* the file is, not where it sits.
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",         # jpeg
    b"\x89PNG\r\n\x1a\n",    # png
    b"GIF87a", b"GIF89a",      # gif
    b"BM",                     # bmp
)


def _is_image_file(path):
    """True only if the file really begins with image magic bytes.

    The extension check in _override exists to give a good error message; this is the
    check that matters, because data/overrides.json can also be edited by hand and the
    art endpoint reads whatever it names.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    if head.startswith(_IMAGE_MAGIC):
        return True
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _within(root, target):
    """True if `target` is `root` or sits underneath it. Case-insensitive."""
    try:
        root = os.path.normcase(os.path.realpath(root))
        target = os.path.normcase(os.path.realpath(target))
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False  # different drives, or a path we cannot resolve


def _art_servable(path):
    """Gate on the bytes leaving /api/art.

    Pointing at any image on disk is a real feature (see the art dialog), but without a
    content check that same field turns the endpoint into an arbitrary file read —
    config.json, data/epic_auth.json, an SSH key. The generated .svg cards are ours and
    live under data/art/.
    """
    if path.lower().endswith(".svg"):
        return _within(config.ART_DIR, path)
    return _is_image_file(path)


def _validate_exe(game, raw):
    """(ok, resolved_path_or_error) for an exe arriving over HTTP.

    A hand-edited overrides.json may point anywhere; that is documented and the file is
    already trusted. A value that arrived over HTTP is not: without this, POSTing an
    absolute exe to /api/override and then calling /api/launch runs any binary on the
    machine, and any page open in the browser can make both calls.
    """
    if not isinstance(raw, str) or not raw.strip():
        return False, "exe must be a path"
    if not raw.lower().endswith(".exe"):
        return False, "executable must be a .exe"

    install_dir = game.get("install_dir")
    if not install_dir:
        return False, "this game has no install folder to pick an executable from"

    # Treat anything already absolute as absolute so it faces the containment check
    # below. Only a drive letter counts on Windows, but under WSL (and in the tests)
    # paths are POSIX, and silently joining those to install_dir would be misleading.
    absolute = bool(winpath.drive_of(raw)) or raw.startswith(("\\\\", "/"))
    candidate = raw if absolute else winpath.join(install_dir, raw)
    target = winpath.native(candidate)
    # realpath resolves junctions, symlinks and the drive-relative "D:foo" form that
    # winpath.drive_of does not treat as absolute, so each of those lands outside the
    # install dir and is refused here rather than launched.
    if not _within(winpath.native(install_dir), target):
        return False, "executable must be inside the game folder"
    if not os.path.isfile(target):
        return False, "no such executable"
    return True, candidate


# -- settings ------------------------------------------------------------

# Keys the settings panel may write. Anything else is rejected rather than ignored, so
# a typo in the UI surfaces instead of quietly landing in config.json.
_SECRET_KEYS = ("steam_api_key", "steamgriddb_key")
# Changing these cannot take effect until the process restarts or install.py re-runs.
_RESTART_KEYS = ("port", "playtime_poll_seconds")
_SHORTCUT_KEYS = ("browser", "window_size")


class Invalid(ValueError):
    """A setting the user can fix, phrased for them rather than for a log."""


def _v_path(value, field):
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    value = value.strip().replace("/", "\\")
    if not value:
        raise Invalid(f"{field} must not be empty")
    if "\x00" in value or "\n" in value:
        raise Invalid(f"{field} contains an illegal character")
    if not winpath.drive_of(value) and not value.startswith("\\\\"):
        raise Invalid(f"{value!r} is not a Windows path (try D:\\Games)")
    # Keep a bare drive root as "D:\"; strip the trailing slash from anything else.
    return value if len(value) <= 3 else value.rstrip("\\")


def _v_paths(value, field):
    if not isinstance(value, list):
        raise Invalid(f"{field} must be a list of folders")
    if len(value) > 64:
        raise Invalid(f"{field}: too many folders")
    out, seen = [], set()
    for item in value:
        path = _v_path(item, field)
        if path.lower() not in seen:
            seen.add(path.lower())
            out.append(path)
    return out


def _v_secret(value, field):
    if not isinstance(value, str):
        raise Invalid(f"{field} must be text")
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise Invalid(f"{field} does not look like a key")
    return value


def _v_bool(value, field):
    if not isinstance(value, bool):
        raise Invalid(f"{field} must be true or false")
    return value


def _v_port(value, field):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise Invalid("port must be a number")
    if not 1024 <= port <= 65535:
        raise Invalid("port must be between 1024 and 65535")
    return port


def _v_steam_id(value, field):
    value = str(value).strip()
    if value and not re.fullmatch(r"\d{1,20}", value):
        raise Invalid("steam_id must be digits only")
    return value


def _v_browser(value, field):
    if value not in ("chrome", "edge", "default"):
        raise Invalid("browser must be chrome, edge or default")
    return value


# Protocols a manually added app may point at. The game schemes are the ones the shortcut
# scanner already accepts; http(s) covers web companions like a tracker site.
_APP_URL = re.compile(
    r"^(steam|com\.epicgames\.launcher|uplay|origin|goggalaxy|riotclient|battlenet|"
    r"roblox-player|minecraft|https?)://", re.I)

_TARGET_EXTS = (".exe", ".lnk", ".url")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Module level so a test can point it somewhere it controls.
_SYSTEM_ROOT = winpath.native(os.environ.get("SystemRoot") or "C:\\Windows")


def _v_target(value, field="target", _depth=0):
    """What a manually added app points at: an executable, a shortcut, or a URL.

    This is the one route by which a request names a program the scanners never found,
    which is the whole point of the feature -- so the target is fenced rather than
    forbidden. That narrows what a bug in the request guard would be worth; it is not
    itself the boundary. `_guard` is.

    Note the ordering: a Windows drive letter is a syntactically valid URL scheme, so
    "C:\\Games\\X.exe" parses as the scheme "c:" and the path branch has to go first.
    """
    if not isinstance(value, str):
        raise Invalid("target must be text")
    value = value.strip()
    if not value:
        raise Invalid("target must not be empty")
    if _CONTROL.search(value) or '"' in value:
        raise Invalid("target contains an illegal character")

    # A leading "/" counts as absolute for the same reason _validate_exe accepts one:
    # under WSL and in the tests paths are POSIX. The checks below -- must exist, must
    # carry a game-shaped extension, must be outside the Windows directory -- are what
    # actually gate this, and they do not care which spelling arrived.
    if winpath.drive_of(value) or value.startswith("\\\\"):
        return _v_target_path(value.replace("/", "\\").rstrip("\\"), "\\", _depth)
    if value.startswith("/"):
        return _v_target_path(value.rstrip("/"), "/", _depth)

    if len(value) > 2048:
        raise Invalid("target is too long")
    if _APP_URL.match(value):
        return "url", value, {}
    raise Invalid("target must be a Windows path, or a steam:// or https:// link")


def _v_target_path(path, sep, _depth):
    if len(path) > 512:
        raise Invalid("target is too long")
    # \\?\ and \\.\ skip path normalisation and reach devices and pipes.
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        raise Invalid("target must be an ordinary file path")
    if any(part in (".", "..") for part in path.split(sep)):
        raise Invalid("target must not contain . or ..")

    low = path.lower()
    if not low.endswith(_TARGET_EXTS):
        raise Invalid("target must be a .exe, or a .lnk / .url shortcut")
    if not os.path.isfile(winpath.native(path)):
        raise Invalid(f"{path} does not exist")
    # One rule instead of a list of names: it rules out cmd.exe, powershell.exe,
    # mshta.exe, rundll32.exe and every other System32 binary worth borrowing. No game
    # has ever lived in the Windows directory.
    if _within(_SYSTEM_ROOT, winpath.native(path)):
        raise Invalid("target must not be inside the Windows directory")

    if low.endswith(".exe"):
        return "exe", path, {}
    # Shortcuts are resolved now rather than launched later: storing the real target is
    # what gives the entry icon art and playtime tracking, and Popen cannot run a .lnk
    # anyway. One hop only -- a shortcut chain is not worth following.
    if _depth:
        raise Invalid("that shortcut points at another shortcut")
    if low.endswith(".lnk"):
        link = shortcut_scanner.resolve_link(path)
        if not link.get("target"):
            raise Invalid("could not read that shortcut's target")
        kind, value, _ = _v_target(link["target"], "target", _depth + 1)
        # A launcher-hosted app is its arguments: the target alone is just the launcher.
        # The icon is a loose .ico for the same reason -- the launcher binary's own icon
        # is the launcher's.
        return kind, value, {"args": link.get("args") or "",
                             "icon": _icon_beside(link.get("icon"), value)}
    found = shortcut_scanner._from_url(winpath.native(path))
    if not (found or {}).get("target"):
        raise Invalid("could not read a link out of that .url file")
    return _v_target(found["target"], "target", _depth + 1)


def _icon_beside(icon, target):
    """A shortcut's own icon file, when we can read it and it is not the target itself.

    IconLocation is "path,index". Only strip a trailing comma group that is actually an
    index -- a directory may legitimately have a comma in its name.
    """
    if not isinstance(icon, str) or not icon.strip():
        return ""
    icon = icon.strip()
    head, sep, tail = icon.rpartition(",")
    if sep and tail.strip().lstrip("-").isdigit():
        icon = head.strip()
    # Same absolute test as _v_target, and for the same reason: production is Windows,
    # but the suite builds real fixtures and runs on Linux too.
    if winpath.drive_of(icon) or icon.startswith("\\\\"):
        icon = icon.replace("/", "\\")
    elif not icon.startswith("/"):
        return ""
    if not icon or icon.lower() == (target or "").lower():
        return ""   # art.py would have found the target's own icon anyway
    if not icon.lower().endswith((".ico", ".exe", ".dll")):
        return ""
    if not os.path.isfile(winpath.native(icon)):
        return ""
    return icon


def _v_args(value, field="args"):
    """Command-line arguments, given as one string, split into an argv list.

    Neither shlex preset works here. posix=True eats the backslashes out of
    "-Dpath=C:\\mc\\lib"; posix=False leaves the quotes attached, and stripping a
    token's outer quotes afterwards mangles '--dir="C:\\Program Files\\X"' into two
    tokens because the quote is in the middle. So configure the lexer instead: posix
    rules for quoting, with backslash disabled as an escape character.
    """
    if value in (None, ""):
        return []
    if isinstance(value, list):
        parts = [str(v) for v in value]
    elif isinstance(value, str):
        if len(value) > 1024:
            raise Invalid("arguments are too long")
        lex = shlex.shlex(value, posix=True)
        lex.whitespace_split = True
        lex.escape = ""        # a Windows path is nothing but backslashes
        lex.commenters = ""    # "#" is a legal character in an argument
        try:
            parts = list(lex)
        except ValueError:
            raise Invalid("arguments have an unclosed quote")
    else:
        raise Invalid("arguments must be text")
    if len(parts) > 32:
        raise Invalid("too many arguments")
    for part in parts:
        if len(part) > 512 or _CONTROL.search(part):
            raise Invalid("arguments contain an illegal value")
    return parts


def _v_app_name(value, field="name"):
    if not isinstance(value, str):
        raise Invalid("name must be text")
    value = " ".join(value.split())
    if not value:
        raise Invalid("name must not be empty")
    if len(value) > 120:
        raise Invalid("name is too long")
    if _CONTROL.search(value):
        raise Invalid("name contains an illegal character")
    return value


def _v_companions(value, game_id, already=()):
    """Ids of other library entries to launch alongside this game.

    An id that is already stored is kept even when it is not in the library right now:
    a folder id is path-derived, so an offline drive makes its game disappear, and
    refusing the save would quietly delete a setup the user still wants.
    """
    if not isinstance(value, list):
        raise Invalid("companions must be a list")
    if len(value) > 8:
        raise Invalid("a game can have at most 8 companion apps")
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 200:
            raise Invalid("each companion must be a game id")
        if item == game_id:
            raise Invalid("a game cannot be its own companion")
        if not find_game(item) and item not in already:
            raise Invalid(f"no such app: {item}")
        if item not in out:
            out.append(item)
    return out


_EDITABLE = {
    "scan_roots": _v_paths,
    "extra_game_dirs": _v_paths,
    "steam_api_key": _v_secret,
    "steamgriddb_key": _v_secret,
    "steam_id": _v_steam_id,
    "include_owned": _v_bool,
    "port": _v_port,
    "browser": _v_browser,
    "steam_root": _v_path,
    "epic_manifests": _v_path,
    "xbox_games": _v_path,
    "riot_root": _v_path,
}


def settings_payload():
    """Current settings, with secrets described rather than disclosed.

    A masked value in the same field invites the client to send the mask back and
    overwrite the real key with bullets, so secrets travel as {set, hint} instead and
    "leave blank to keep" is the natural default.
    """
    cfg = config.load()
    values = {k: cfg.get(k) for k in _EDITABLE if k not in _SECRET_KEYS}
    secrets = {}
    for key in _SECRET_KEYS:
        raw = (cfg.get(key) or "").strip()
        secrets[key] = {"set": bool(raw), "hint": raw[-4:] if len(raw) > 4 else ""}
    return {
        "config": values,
        "secrets": secrets,
        "defaults": {k: config.DEFAULTS.get(k) for k in _EDITABLE},
        "config_path": winpath.windows(config.CONFIG_JSON),
    }


def write_settings(payload):
    """Validate and merge into config.json. Returns (response, error)."""
    if not isinstance(payload, dict):
        return None, "expected an object"

    changes = {}
    for key, value in payload.items():
        if key not in _EDITABLE:
            return None, f"unknown setting: {key}"
        # A secret left out, or sent as null, keeps whatever is already stored.
        if key in _SECRET_KEYS and value is None:
            continue
        try:
            changes[key] = _EDITABLE[key](value, key)
        except Invalid as exc:
            return None, str(exc)

    stored = config.read_json(config.CONFIG_JSON, {})
    if not isinstance(stored, dict):
        stored = {}
    before = config.load()
    stored.update(changes)
    config.write_json(config.CONFIG_JSON, stored)

    changed = [k for k, v in changes.items() if before.get(k) != v]
    # Folders that do not exist are allowed -- a drive can be offline -- but say so. A
    # path that exists and is not a directory is a different mistake, and pointing at an
    # executable is common enough to name the fix.
    warnings = []
    for key in ("scan_roots", "extra_game_dirs"):
        for path in changes.get(key, []):
            if winpath.isdir(path):
                continue
            warnings.append(f"{path} is a file, not a folder \u2014 add it with \u201c+ Add\u201d"
                            if winpath.exists(path)
                            else f"{path} does not exist right now")
    if any(k in changed for k in _SECRET_KEYS):
        art.reset_network_state()

    return {
        "ok": True,
        "changed": changed,
        "warnings": warnings,
        "restart_required": [k for k in changed if k in _RESTART_KEYS],
        "reinstall_required": [k for k in changed if k in _SHORTCUT_KEYS],
    }, None


# -- manually added apps -------------------------------------------------

# Entries the user creates live in overrides.json under "extra_games", the same reserved
# list a hand-edited file may use. Only ids under this prefix are reachable over HTTP,
# which is what keeps /api/apps from being a way to write an arbitrary id into a list
# apply_overrides reads as a list.
_MANUAL_PREFIX = "manual:"


def _extra_games(overrides):
    """The extra_games list, as a copy we can mutate.

    A hand-edited file may have it as something other than a list. Overwriting that would
    throw the user's data away, so the caller turns None into a 400 instead.
    """
    raw = overrides.get("extra_games")
    if raw is None:
        return []
    return list(raw) if isinstance(raw, list) else None


def _manual_id(name, taken):
    """manual: plus a slug of the name, in the idiom the shortcut scanner already uses."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = f"app-{int(time.time())}"
    candidate = f"{_MANUAL_PREFIX}{slug}"
    n = 2
    while candidate in taken:
        candidate = f"{_MANUAL_PREFIX}{slug}-{n}"
        n += 1
    return candidate


def _name_conflict(name, own_id):
    """The game already holding this name, normalised the way the scan dedupes.

    extra_games are merged *after* dedupe_by_name, so nothing downstream would collapse a
    collision -- it would just sit there as two identical tiles and fail --selftest.
    """
    key = scan._NAME_KEY.sub("", name.lower())
    if not key:
        return None
    for game in STATE["library"].get("games", []):
        if game["id"] != own_id and scan._NAME_KEY.sub("", game["name"].lower()) == key:
            return game
    return None


def _install_manual_record(entry, overrides):
    """Put a saved entry into the live library without waiting for a rescan.

    The dict already in STATE["library"] is the one find_game hands out, so an edit
    updates it in place rather than replacing it.
    """
    games = STATE["library"].setdefault("games", [])
    fresh = scan._blank_record(entry)
    record = next((g for g in games if g["id"] == fresh["id"]), None)
    if record is None:
        record = fresh
        games.append(record)
    else:
        record.clear()
        record.update(fresh)
    # apply_overrides appends every other extra_games entry to the list it is given, so
    # hand it a throwaway rather than the library.
    scan.apply_overrides([record], overrides)
    games.sort(key=lambda g: g["name"].lower())
    STATE["library"]["count"] = len(games)
    config.write_json(config.LIBRARY_JSON, STATE["library"])
    return record


def launch_companions(game):
    """Start the apps configured to run alongside `game`; returns (started, problems).

    The game itself has already been launched by the time this runs, so a companion that
    fails is reported rather than raised -- a broken helper must never cost you the thing
    you actually clicked. Companions are not handed to the playtime tracker: watches are
    keyed by executable name and a helper left running all day would otherwise log a
    session against itself, and against the game when the two share a name.
    """
    also, failed = [], []
    for mate_id in game.get("companions") or []:
        mate = find_game(mate_id)
        if not mate:
            # Folder ids are path-derived, so moving a folder renames its game. Say so
            # rather than quietly dropping it: the drive may simply be offline.
            failed.append(f"{mate_id} is no longer in your library")
            continue
        try:
            # One level only. A companion's own companions are never followed, which is
            # what makes a cycle impossible to build rather than merely unlikely.
            launcher.launch(mate)
        except (launcher.LaunchError, OSError) as exc:
            failed.append(f"{mate['name']}: {exc}")
            continue
        also.append(mate["name"])
        print(f"[launch]   + {mate['name']}")
    return also, failed


def _drop_companion(overrides, game_id):
    """Remove a deleted app from every rule that listed it as a companion."""
    for key, rule in list(overrides.items()):
        if key in ("extra_games", "owned_games") or not isinstance(rule, dict):
            continue
        mates = rule.get("companions")
        if not isinstance(mates, list) or game_id not in mates:
            continue
        kept = [c for c in mates if c != game_id]
        if kept:
            rule["companions"] = kept
        else:
            rule.pop("companions", None)
        if not rule:
            overrides.pop(key, None)


# -- HTTP ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "GameDashboard/1.0"
    # Without this the default is HTTP/1.0, i.e. Connection: close, so every one of the
    # tiles opens its own TCP connection and a full art reload takes about a second.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # the default logger writes a line per request; too noisy

    # -- helpers --

    def _send(self, status, body, content_type="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path, cache=False, extra=None):
        try:
            stamp = os.path.getmtime(path)
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})

        headers = dict(extra or {})
        if cache:
            headers.setdefault("Cache-Control", "public, max-age=86400")
        etag = '"%x-%x"' % (int(stamp), len(body))
        headers["ETag"] = etag

        # A tile that is still showing the right image must not be re-sent; that round
        # trip is what makes the grid blink when it re-renders.
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return self._send(200, body, ctype, headers)

    def _refuse(self, status, message):
        """Reject a request without having read its body.

        The body bytes are still sitting in the socket, and this connection is keep-alive
        (HTTP/1.1, which the grid depends on). Leaving them there desynchronises the
        parser, so the *next* request on the connection starts mid-body and comes back
        as a nonsense 501. Closing is simpler and cheaper than draining an arbitrary
        body from a caller we have already decided not to trust.
        """
        self.close_connection = True
        self._send(status, {"error": message}, extra={"Connection": "close"})
        return False

    def _guard(self):
        """Refuse anything a page on another origin could have aimed at us.

        Binding loopback keeps the network out, but it is not a boundary against the
        user's own browser: any site they have open can POST to 127.0.0.1. Requiring a
        JSON content type is what forces a CORS preflight on those requests, and since
        no CORS headers are ever emitted the preflight fails and the request never
        arrives. Without it they are "simple requests" that get sent regardless of
        whether the other origin may read the reply — and override -> launch never
        needed to read one.
        """
        port = STATE.get("port") or STATE["cfg"].get("port")
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

        # No Host match -> a DNS-rebinding name is pointing at us.
        if (self.headers.get("Host") or "").strip() not in hosts:
            return self._refuse(403, "bad host")

        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin not in {f"http://{h}" for h in hosts}:
            return self._refuse(403, "cross-origin request refused")

        # Modern browsers state the relationship themselves. Absent on curl and other
        # non-browser clients, so this only ever tightens the browser case.
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return self._refuse(403, "cross-site request refused")

        if self.command == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype.lower() != "application/json":
                return self._refuse(415, "expected Content-Type: application/json")
        return True

    _MAX_BODY = 1 << 20

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > self._MAX_BODY:
                return {}
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}
        # Every caller does payload.get(...); a list or string body would raise there.
        return payload if isinstance(payload, dict) else {}

    # -- routes --

    def do_GET(self):
        if not self._guard():
            return None
        parsed = urlparse(self.path)
        route = posixpath.normpath(parsed.path)
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._send_file(os.path.join(config.WEB_DIR, "index.html"))

        if route == "/api/games":
            return self._send(200, games_payload())

        if route == "/api/art":
            return self._art(query.get("id", [""])[0])

        if route == "/api/candidates":
            return self._candidates(query.get("id", [""])[0])

        if route == "/api/settings":
            return self._send(200, settings_payload())

        if route == "/api/detect":
            return self._detect(query.get("refresh", [""])[0] == "1")

        if route.startswith("/api/"):
            return self._send(404, {"error": "unknown endpoint"})

        # Static assets, confined to web/. The containment test is a path comparison,
        # not a string prefix: "web-backup" starts with "web" but is not inside it.
        rel = route.lstrip("/")
        target = os.path.join(config.WEB_DIR, rel)
        if not _within(config.WEB_DIR, target) or not os.path.isfile(target):
            return self._send(404, {"error": "not found"})
        return self._send_file(target, cache=True)

    def do_POST(self):
        if not self._guard():
            return None
        route = posixpath.normpath(urlparse(self.path).path)
        payload = self._body_json()

        if route == "/api/launch":
            return self._launch(payload.get("id"))
        if route == "/api/reveal":
            return self._reveal(payload.get("id"))
        if route == "/api/rescan":
            threading.Thread(target=load_library, args=(True,), daemon=True).start()
            return self._send(200, {"ok": True, "scanning": True})
        if route == "/api/sizes":
            threading.Thread(target=compute_sizes, daemon=True).start()
            return self._send(200, {"ok": True, "sizing": True})
        if route == "/api/override":
            return self._override(payload)
        if route == "/api/apps":
            return self._apps(payload)
        if route == "/api/apps/remove":
            return self._apps_remove(payload)
        if route == "/api/settings":
            return self._settings(payload)
        return self._send(404, {"error": "unknown endpoint"})

    # -- handlers --

    def _launch(self, game_id):
        game = find_game(game_id)
        if not game:
            return self._send(404, {"error": "no such game"})
        try:
            what = launcher.launch(game)
        except launcher.LaunchError as exc:
            return self._send(400, {"error": str(exc)})
        except OSError as exc:
            return self._send(500, {"error": f"launch failed: {exc}"})

        if STATE["tracker"]:
            STATE["tracker"].watch(game)
        print(f"[launch] {game['name']} -> {what}")

        also, failed = launch_companions(game)
        return self._send(200, {"ok": True, "launched": what,
                                "also": also, "failed": failed})

    def _reveal(self, game_id):
        game = find_game(game_id)
        if not game:
            return self._send(404, {"error": "no such game"})
        try:
            launcher.reveal(game)
        except (launcher.LaunchError, OSError) as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"ok": True})

    def _art(self, game_id):
        game = find_game(game_id)
        if not game:
            return self._send(404, {"error": "no such game"})
        try:
            path, kind = art.resolve(game, STATE["cfg"])
        except Exception as exc:
            print(f"[art] {game['name']}: {exc}")
            path, kind = None, "placeholder"

        if path and not _art_servable(path):
            # An art_override can be hand-written into overrides.json, so the bytes are
            # only served once they are confirmed to be an image.
            print(f"[art] refusing to serve non-image {path}")
            path = None

        if path:
            return self._send_file(path, cache=True)
        return self._send(
            200, art.placeholder_svg(game["name"]), "image/svg+xml",
            {"Cache-Control": "public, max-age=3600", "X-Art-Kind": kind},
        )

    def _candidates(self, game_id):
        game = find_game(game_id)
        if not game or not game.get("install_dir"):
            return self._send(404, {"error": "no folder for this game"})
        found = exefind.candidates(game["install_dir"], game["name"], limit=30)
        return self._send(200, {"candidates": found})

    def _detect(self, refresh):
        """Detection is a few seconds of disk work, so the answer is kept."""
        cached = STATE.get("detected")
        if cached and not refresh:
            return self._send(200, cached)
        try:
            found = detect.run(STATE["cfg"])
        except Exception as exc:
            print(f"[detect] failed: {exc}")
            found = {"candidates": [], "launchers": {}, "drives": [], "error": str(exc)}
        STATE["detected"] = found
        return self._send(200, found)

    def _settings(self, payload):
        result, error = write_settings(payload)
        if error:
            return self._send(400, {"error": error})
        # Reload and rescan exactly as /api/rescan does; load_library re-reads config.
        STATE["detected"] = None
        threading.Thread(target=load_library, args=(True,), daemon=True).start()
        result["scanning"] = True
        return self._send(200, result)

    def _apps(self, payload):
        """Create or update an entry in overrides.json's extra_games list.

        This cannot ride on /api/override: that route refuses any id not already in the
        library precisely so a request can never write into the reserved list keys. So
        the rule here is the mirror image -- an update may only touch an id that is
        already an entry in extra_games, which keeps owned_games and every scanner-found
        game out of reach, and gives the product rule "you can only remove what you
        added". Everything else is Hide, because a rescan would bring it back anyway.
        """
        game_id = payload.get("id")
        if game_id is not None and not isinstance(game_id, str):
            return self._send(400, {"error": "id must be text"})
        try:
            name = _v_app_name(payload.get("name"))
            kind, target, extra = _v_target(payload.get("target"))
            # A shortcut's own arguments come first: they are what identifies the app,
            # and anything the user typed is additional.
            args = _v_args(extra.get("args")) + _v_args(payload.get("args"))
        except Invalid as exc:
            return self._send(400, {"error": str(exc)})

        with _lock:
            overrides = config.read_json(config.OVERRIDES_JSON, {})
            if not isinstance(overrides, dict):
                return self._send(400, {"error": "data/overrides.json is not an object"})
            extras = _extra_games(overrides)
            if extras is None:
                return self._send(400, {
                    "error": "data/overrides.json has a malformed extra_games list — "
                             "fix it by hand"})

            if game_id:
                if not game_id.startswith(_MANUAL_PREFIX):
                    return self._send(400, {"error": "only apps you added can be edited"})
                entry = next((e for e in extras
                              if isinstance(e, dict) and e.get("id") == game_id), None)
                if entry is None:
                    return self._send(404, {"error": "no such app"})
            else:
                taken = {e.get("id") for e in extras if isinstance(e, dict)}
                taken |= {g["id"] for g in STATE["library"].get("games", [])}
                taken |= set(overrides)
                entry = {"id": _manual_id(name, taken)}
                extras.append(entry)
                game_id = entry["id"]

            clash = _name_conflict(name, game_id)
            if clash:
                return self._send(400, {
                    "error": f"\u201c{clash['name']}\u201d is already in your library "
                             f"(from {clash['source']}) — pick a different name"})

            was = entry.get("exe") or entry.get("url")
            was_icon = entry.get("icon")
            entry["name"] = name
            for stale in ("exe", "url", "args", "icon"):
                entry.pop(stale, None)
            if kind == "exe":
                entry["exe"] = target
                if args:
                    entry["args"] = args
            else:
                entry["url"] = target
            if extra.get("icon"):
                entry["icon"] = extra["icon"]
            elif was_icon and was == target:
                # Editing the name of an entry added from a shortcut must not cost it the
                # icon it came with -- the Target field shows the resolved exe by then.
                entry["icon"] = was_icon
            overrides["extra_games"] = extras

            rule = overrides.get(game_id)
            rule = dict(rule) if isinstance(rule, dict) else {}
            # A name set here is the entry's own; a leftover Rename rule would be applied
            # on top of it and make editing the name look like it did nothing.
            rule.pop("name", None)
            rule.pop("exe", None)
            if payload.get("visible") is False:
                rule["hidden"] = True
            else:
                rule.pop("hidden", None)
            if rule:
                overrides[game_id] = rule
            else:
                overrides.pop(game_id, None)

            config.write_json(config.OVERRIDES_JSON, overrides)
            if was != target:
                _forget_art(game_id)   # or a re-pointed entry keeps the old icon card
            record = _install_manual_record(entry, overrides)

        warnings = ([] if kind == "exe" else
                    [f"{name} has no executable, so it gets no cover art from its icon "
                     f"and no playtime tracking"])
        return self._send(200, {"ok": True, "id": game_id, "warnings": warnings,
                                "game": record})

    def _apps_remove(self, payload):
        game_id = payload.get("id")
        if not isinstance(game_id, str) or not game_id.startswith(_MANUAL_PREFIX):
            return self._send(400, {"error": "only apps you added can be removed"})

        with _lock:
            overrides = config.read_json(config.OVERRIDES_JSON, {})
            if not isinstance(overrides, dict):
                return self._send(400, {"error": "data/overrides.json is not an object"})
            extras = _extra_games(overrides)
            if extras is None:
                return self._send(400, {
                    "error": "data/overrides.json has a malformed extra_games list — "
                             "fix it by hand"})
            kept = [e for e in extras
                    if not (isinstance(e, dict) and e.get("id") == game_id)]
            if len(kept) == len(extras):
                return self._send(404, {"error": "no such app"})

            overrides["extra_games"] = kept
            overrides.pop(game_id, None)
            _drop_companion(overrides, game_id)
            config.write_json(config.OVERRIDES_JSON, overrides)

            games = [g for g in STATE["library"].get("games", []) if g["id"] != game_id]
            for game in games:
                mates = game.get("companions")
                if mates and game_id in mates:
                    game["companions"] = [c for c in mates if c != game_id]
            STATE["library"]["games"] = games
            STATE["library"]["count"] = len(games)
            config.write_json(config.LIBRARY_JSON, STATE["library"])

        # playtime.json keeps its entry on purpose: the id is a deterministic slug of the
        # name, so re-adding the same app gets its history back.
        _forget_art(game_id)
        return self._send(200, {"ok": True, "removed": game_id})

    def _override(self, payload):
        game_id = payload.get("id")
        if not isinstance(game_id, str) or not game_id:
            return self._send(400, {"error": "id required"})

        # Only ever override a game that exists. That is all the UI does, and it also
        # keeps the reserved keys apply_overrides reads as lists (extra_games,
        # owned_games) out of reach — a dict written to one of those breaks every
        # later scan with an AttributeError until the file is repaired by hand.
        game = find_game(game_id)
        if not game:
            return self._send(404, {"error": "no such game"})

        if payload.get("exe"):
            ok, resolved = _validate_exe(game, payload["exe"])
            if not ok:
                return self._send(400, {"error": resolved})
            payload = dict(payload, exe=resolved)

        if payload.get("steam_appid") is not None:
            try:
                appid = int(payload["steam_appid"])
            except (TypeError, ValueError):
                return self._send(400, {"error": "steam_appid must be a number"})
            if not 1 <= appid <= 20_000_000:
                return self._send(400, {"error": "steam_appid out of range"})
            payload = dict(payload, steam_appid=appid)

        if "companions" in payload:
            stored = config.read_json(config.OVERRIDES_JSON, {})
            saved = stored.get(game_id) if isinstance(stored, dict) else None
            already = (saved or {}).get("companions") or []
            try:
                payload = dict(payload, companions=_v_companions(
                    payload["companions"], game_id, already))
            except Invalid as exc:
                return self._send(400, {"error": str(exc)})

        if payload.get("art"):
            art_path = payload["art"]
            if not isinstance(art_path, str) or not art_path.lower().endswith(_IMAGE_EXTS):
                return self._send(400, {"error": "cover art must be an image file"})
            if not _is_image_file(winpath.native(art_path)):
                return self._send(400, {"error": "that file is not a readable image"})

        overrides = config.read_json(config.OVERRIDES_JSON, {})
        rule = overrides.get(game_id, {})
        for field in ("name", "exe", "steam_appid", "art", "hidden", "companions"):
            if field in payload:
                value = payload[field]
                # [] is how the companion picker says "none"; no other field is a list.
                if value in (None, "", []):
                    rule.pop(field, None)
                else:
                    rule[field] = value
        if rule:
            overrides[game_id] = rule
        else:
            overrides.pop(game_id, None)
        config.write_json(config.OVERRIDES_JSON, overrides)

        # Re-apply in place so the UI updates without a full rescan.
        if "steam_appid" in payload or "art" in payload:
            _forget_art(game_id)
        # apply_overrides is written for freshly built records: it only ever *sets* these
        # fields, never clears them. Re-applying in place has to hand it a record in the
        # same state, or a cleared rule leaves the last value stuck until the next scan --
        # which is why Unhide appeared to do nothing until a rescan.
        for derived in ("hidden", "art_override", "companions"):
            game.pop(derived, None)
        scan.apply_overrides([game], overrides)
        config.write_json(config.LIBRARY_JSON, STATE["library"])
        return self._send(200, {"ok": True, "override": rule})


def _forget_art(game_id):
    """Drop cached art so a corrected appid takes effect immediately."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", game_id)
    for ext in (".jpg", ".png", ".card.svg", ".miss"):
        try:
            os.remove(os.path.join(config.ART_DIR, safe + ext))
        except OSError:
            pass


def serve(port=None, open_browser=False):
    config.safe_console()
    config.ensure_dirs()
    cfg = config.load()
    port = port or cfg["port"]

    STATE["tracker"] = playtime.Tracker(cfg.get("playtime_poll_seconds", 15))
    STATE["tracker"].start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    STATE["port"] = port
    print(f"[server] http://127.0.0.1:{port}")

    # Bind first, scan second. A first run with no data/library.json can spend minutes
    # inside owned_steam's store lookups, and doing that before the socket exists means
    # the window opens on a dead port with no console (pythonw) to explain why. The
    # payload already carries a "scanning" flag, so the page loads and fills in.
    threading.Thread(
        target=load_library,
        args=(not os.path.exists(config.LIBRARY_JSON),),
        daemon=True,
    ).start()

    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open,
                        args=(f"http://127.0.0.1:{port}",)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Run the game dashboard server.")
    ap.add_argument("--port", type=int)
    ap.add_argument("--open", action="store_true", help="open a browser on start")
    args = ap.parse_args()
    serve(args.port, args.open)
