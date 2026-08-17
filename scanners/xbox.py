"""Xbox / Microsoft Store games.

Store apps have no launchable path on disk; they start through their AppUserModelID
via `explorer.exe shell:AppsFolder\\<AUMID>`. AUMIDs come from Get-StartApps.
"""

import difflib
import os
import re

import winpath
import winshell

SOURCE = "xbox"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Store entries that are launchers/tools rather than games we want listed twice.
_SKIP = re.compile(r"^(xbox|microsoft store|game bar|gaming services)", re.I)


def _norm(text):
    return _NON_ALNUM.sub("", (text or "").lower())


def _start_apps():
    return winshell.run_json("Get-StartApps | Select-Object Name, AppID")


def scan(cfg):
    root = cfg["xbox_games"]
    native = winpath.native(root)
    if not os.path.isdir(native):
        return []

    dirs = [d for d in sorted(os.listdir(native))
            if os.path.isdir(os.path.join(native, d)) and not d.startswith("$")
            and d.lower() != "gamesave"]
    if not dirs:
        return []

    apps = _start_apps()
    # Store AUMIDs contain '!'; plain Win32 Start Menu entries point at .lnk files.
    store_apps = [a for a in apps if isinstance(a, dict) and "!" in str(a.get("AppID", ""))]

    games = []
    for name in dirs:
        aumid = _match_aumid(name, store_apps)
        if not aumid:
            print(f"[xbox] no AUMID for {name!r}; skipping")
            continue
        games.append({
            "id": f"xbox:{aumid}",
            "name": name,
            "source": SOURCE,
            "install_dir": winpath.join(root, name),
            "launch": {"kind": "shell", "value": aumid},
            "exe_name": None,
            "exe_path": None,
            "size_bytes": 0,
            "last_played": None,
            "steam_appid": None,
        })
    return games


def _match_aumid(folder_name, store_apps):
    target = _norm(folder_name)
    best, best_score = None, 0.0
    for app in store_apps:
        score = difflib.SequenceMatcher(None, _norm(app.get("Name")), target).ratio()
        if score > best_score:
            best, best_score = app, score
    if best and best_score >= 0.6 and not _SKIP.match(best.get("Name") or ""):
        return best.get("AppID")
    return None
