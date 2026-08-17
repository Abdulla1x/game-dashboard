"""Playtime tracking for games no launcher tracks.

Launched processes are not our children — Steam, Epic and Riot all re-parent them — so
sessions are tracked by executable name via `tasklist`. A game is watched from the moment
it is launched; the first sighting opens a session and the first absence after that closes
it.
"""

import subprocess
import threading
import time

import config
import winpath

_NO_WINDOW = 0x08000000
_CWD = None if winpath.ON_WINDOWS else "/mnt/c"

# How long to keep looking for a process after launch before giving up on it.
_STARTUP_GRACE = 180


def running_processes():
    """Lowercased set of running executable names."""
    try:
        proc = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30, cwd=_CWD,
            creationflags=_NO_WINDOW if winpath.ON_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    names = set()
    for line in proc.stdout.splitlines():
        if line.startswith('"'):
            names.add(line.split('","', 1)[0][1:].lower())
    return names


class Tracker:
    """Polls for watched executables and records sessions to playtime.json."""

    def __init__(self, poll_seconds=15):
        self.poll_seconds = max(5, int(poll_seconds))
        self._lock = threading.Lock()
        self._watch = {}      # exe_name(lower) -> {game_id, since, started}
        self._data = config.read_json(config.PLAYTIME_JSON, {})
        self._thread = None

    # -- public API ------------------------------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def watch(self, game):
        """Begin looking for this game's process after a launch."""
        exe = (game.get("exe_name") or "").lower()
        if not exe:
            return
        with self._lock:
            self._watch[exe] = {
                "game_id": game["id"],
                "since": time.time(),
                "started": None,
            }

    def stats(self):
        with self._lock:
            return dict(self._data)

    def for_game(self, game_id):
        with self._lock:
            entry = self._data.get(game_id) or {}
        return {
            "total_seconds": entry.get("total_seconds", 0),
            "sessions": len(entry.get("sessions", [])),
            "last_played": entry.get("last_played"),
        }

    def active_ids(self):
        with self._lock:
            return {w["game_id"] for w in self._watch.values() if w["started"]}

    # -- internals -------------------------------------------------------

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception as exc:
                print(f"[playtime] {exc}")
            time.sleep(self.poll_seconds)

    def _tick(self):
        with self._lock:
            watching = dict(self._watch)
        if not watching:
            return

        live = running_processes()
        now = time.time()
        finished = []

        with self._lock:
            for exe, state in list(self._watch.items()):
                if exe in live:
                    if state["started"] is None:
                        state["started"] = now
                        print(f"[playtime] {state['game_id']} started")
                elif state["started"] is not None:
                    finished.append((state["game_id"], state["started"], now))
                    del self._watch[exe]
                elif now - state["since"] > _STARTUP_GRACE:
                    # Never appeared: wrong exe name, or the user closed it instantly.
                    del self._watch[exe]

        for game_id, started, ended in finished:
            self._record(game_id, started, ended)

    def _record(self, game_id, started, ended):
        duration = int(ended - started)
        if duration < 30:
            return  # ignore crashes and instant quits
        with self._lock:
            entry = self._data.setdefault(
                game_id, {"total_seconds": 0, "sessions": []}
            )
            entry["total_seconds"] += duration
            entry["last_played"] = int(ended)
            entry["sessions"].append({"start": int(started), "end": int(ended)})
            entry["sessions"] = entry["sessions"][-200:]
            snapshot = dict(self._data)
        config.write_json(config.PLAYTIME_JSON, snapshot)
        print(f"[playtime] {game_id} +{duration // 60}m")
