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
    # Folders that do not exist are allowed -- a drive can be offline -- but say so.
    warnings = [f"{p} does not exist right now"
                for k in ("scan_roots", "extra_game_dirs") if k in changes
                for p in changes[k] if not winpath.isdir(p)]
    if any(k in changed for k in _SECRET_KEYS):
        art.reset_network_state()

    return {
        "ok": True,
        "changed": changed,
        "warnings": warnings,
        "restart_required": [k for k in changed if k in _RESTART_KEYS],
        "reinstall_required": [k for k in changed if k in _SHORTCUT_KEYS],
    }, None


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
        return self._send(200, {"ok": True, "launched": what})

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

        if payload.get("art"):
            art_path = payload["art"]
            if not isinstance(art_path, str) or not art_path.lower().endswith(_IMAGE_EXTS):
                return self._send(400, {"error": "cover art must be an image file"})
            if not _is_image_file(winpath.native(art_path)):
                return self._send(400, {"error": "that file is not a readable image"})

        overrides = config.read_json(config.OVERRIDES_JSON, {})
        rule = overrides.get(game_id, {})
        for field in ("name", "exe", "steam_appid", "art", "hidden"):
            if field in payload:
                value = payload[field]
                if value in (None, ""):
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
