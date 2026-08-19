"""Project paths and user configuration."""

import json
import os
import sys

import winpath

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
WEB_DIR = os.path.join(ROOT, "web")
ART_DIR = os.path.join(DATA_DIR, "art")

LIBRARY_JSON = os.path.join(DATA_DIR, "library.json")
OVERRIDES_JSON = os.path.join(DATA_DIR, "overrides.json")
PLAYTIME_JSON = os.path.join(DATA_DIR, "playtime.json")
APPLIST_JSON = os.path.join(DATA_DIR, "steam_applist.json")
CONFIG_JSON = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "port": 8777,
    # "chrome" | "edge" | "default" — see the Taskbar section of the plan.
    "browser": "chrome",
    "window_size": "1400,900",
    "playtime_poll_seconds": 15,
    # Directories whose immediate children are candidate game folders. Empty by
    # default: `detect.py` proposes real ones from this machine on first run, and the
    # settings panel writes the chosen set to config.json.
    "scan_roots": [],
    # Individual game folders that are not children of a scan root.
    "extra_game_dirs": [],
    # Folder names skipped during scanning (case-insensitive). The capture-tool names
    # matter more than they look: clip recorders create a folder per game, and those
    # folders sit right next to real installs.
    "ignore_dirs": [
        "$RECYCLE.BIN", "System Volume Information", "Recovery", "Boot",
        "Config.Msi", "WUDownloadCache", "_Redist", "_CommonRedist",
        "SteamLibrary", "Epic Games", "Battle.net", "Riot Games",
        "Medal", "Radeon ReLive", "Captures", "Clips", "Recordings",
        "replay_cache", "thumb_cache", "New folder",
    ],
    # Pull in games you own but have not installed. They show up only under the
    # dashboard's "All" view, and clicking one hands the download to its launcher.
    "include_owned": True,
    # Owned Steam games come from the Web API when these two are set (free key from
    # steamcommunity.com/dev/apikey). Without them the library is inferred from Steam's
    # own on-disk cache, which is workable but noisier.
    "steam_api_key": "",
    "steam_id": "",
    # Free key from steamgriddb.com/profile/preferences/api — supplies portrait covers
    # for everything Steam has no app ID for (Epic/Riot titles, launchers, mod clients).
    "steamgriddb_key": "",
    "steam_root": "C:\\Program Files (x86)\\Steam",
    "epic_manifests": "C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\Manifests",
    "xbox_games": "C:\\XboxGames",
    "riot_root": "C:\\Riot Games",
    # Only used when running from WSL, where %APPDATA% cannot be expanded. On Windows
    # the real username comes from the environment; see `windows_user()`.
    "windows_user": "",
}


def windows_user(cfg=None):
    """The Windows account name, for building %APPDATA% paths.

    On Windows this is just the environment. It only needs configuring under WSL, where
    %APPDATA% cannot be expanded and there is nothing to infer it from.
    """
    if winpath.ON_WINDOWS:
        name = os.environ.get("USERNAME") or os.environ.get("USER")
        if name:
            return name
    name = (cfg or {}).get("windows_user") or os.environ.get("WINDOWS_USER")
    if not name:
        raise RuntimeError(
            "Cannot tell which Windows user to use. Running outside Windows? "
            'Set "windows_user" in config.json to your Windows account name.')
    return name


def load():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_JSON):
        try:
            with open(CONFIG_JSON, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            # A top-level list or number would make cfg.update raise TypeError and take
            # the server down before it binds. It is a hand-edited file, and now also a
            # file the settings panel writes, so it gets checked rather than trusted.
            if isinstance(loaded, dict):
                cfg.update(loaded)
            else:
                print(f"[config] config.json should hold an object, got "
                      f"{type(loaded).__name__} — using defaults")
        except (OSError, ValueError) as exc:
            print(f"[config] ignoring bad config.json: {exc}")
    return cfg


def read_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def safe_console():
    """Stop a non-ASCII game name from killing a console run.

    The Windows console is cp1252 by default, and the owned-library scanners surface
    titles it cannot encode (CJK names, typographic quotes). Without this, printing the
    scan table raises UnicodeEncodeError and takes the whole run with it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a real console (pythonw, a pipe); nothing to protect


def ensure_dirs():
    for d in (DATA_DIR, ART_DIR):
        os.makedirs(d, exist_ok=True)
