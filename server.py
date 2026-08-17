"""Local HTTP server for the dashboard.

Binds 127.0.0.1 only and has no authentication — it can start arbitrary executables,
so it must never be reachable from the network.
"""

import json
import mimetypes
import os
import posixpath
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import art
import config
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
        out.append({
            **game,
            "running": game["id"] in active,
            "playtime_seconds": tracked_total,
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


# -- HTTP ----------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "GameDashboard/1.0"

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

    def _send_file(self, path, cache=False):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        extra = {"Cache-Control": "public, max-age=86400"} if cache else {}
        return self._send(200, body, ctype, extra)

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}

    # -- routes --

    def do_GET(self):
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

        if route.startswith("/api/"):
            return self._send(404, {"error": "unknown endpoint"})

        # Static assets, confined to web/.
        rel = route.lstrip("/")
        target = os.path.normpath(os.path.join(config.WEB_DIR, rel))
        if not target.startswith(config.WEB_DIR) or not os.path.isfile(target):
            return self._send(404, {"error": "not found"})
        return self._send_file(target, cache=True)

    def do_POST(self):
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

    def _override(self, payload):
        game_id = payload.get("id")
        if not game_id:
            return self._send(400, {"error": "id required"})

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
        game = find_game(game_id)
        if game:
            if "steam_appid" in payload or "art" in payload:
                _forget_art(game_id)
            scan.apply_overrides([game], overrides)
            config.write_json(config.LIBRARY_JSON, STATE["library"])
        return self._send(200, {"ok": True, "override": rule})


def _forget_art(game_id):
    """Drop cached art so a corrected appid takes effect immediately."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", game_id)
    for ext in (".jpg", ".png", ".miss"):
        try:
            os.remove(os.path.join(config.ART_DIR, safe + ext))
        except OSError:
            pass


def serve(port=None, open_browser=False):
    config.ensure_dirs()
    cfg = config.load()
    port = port or cfg["port"]

    STATE["tracker"] = playtime.Tracker(cfg.get("playtime_poll_seconds", 15))
    STATE["tracker"].start()

    load_library(rescan=not os.path.exists(config.LIBRARY_JSON))
    count = len(STATE["library"].get("games", []))

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[server] {count} games — http://127.0.0.1:{port}")

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
