"""Start Menu shortcuts — the catch-all for games no other scanner reaches.

Runs last. The Start Menu is mostly *not* games (Office, browsers, dev tools), so this
scanner is deliberately conservative: a shortcut is only accepted when its target looks
like a game by location or publisher, and never when a denylist pattern matches.
Anything it gets wrong is one click to hide.
"""

import os
import re

import winpath
import winshell

SOURCE = "shortcut"

_START_MENUS = [
    "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs",
    "C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs",
]

# Never a game, however it scores.
_DENY = re.compile(
    r"uninstall|readme|read me|help|documentation|manual|website|web site|support|"
    r"release notes|changelog|repair|crash|report|settings|configur|troubleshoot|"
    r"\bword\b|excel|powerpoint|outlook|onenote|publisher|access|office|onedrive|"
    r"edge|chrome|firefox|opera|brave|internet explorer|"
    r"visual studio|vs code|\bcode\b|\bgit\b|python|node|java|mysql|docker|terminal|"
    r"powershell|command prompt|notepad|calculator|paint|character map|"
    r"obs studio|discord|spotify|zoom|webex|teams|vlc|7-zip|winrar|wiztree|"
    r"afterburner|rivatuner|openrgb|capcut|vegas|obsidian|ollama|lm studio|stremio|"
    r"medal|overwolf|faceit|tracker|driver|amd |intel |nvidia|control panel|"
    r"file explorer|task manager|defender|update|installer|setup|bug report",
    re.I,
)

# Directory fragments that mark a target as game-ish.
_GAME_LOCATIONS = re.compile(
    r"\\(games|steamlibrary|steamapps|epic games|riot games|rockstar games|"
    r"ubisoft|battle\.net|ea games|origin games|gog galaxy|loading ?bay|"
    r"xboxgames|roblox|minecraft|curseforge|fortnite)\b",
    re.I,
)

# Game protocols in .url files.
_GAME_URL = re.compile(
    r"^(steam|com\.epicgames\.launcher|uplay|origin|goggalaxy|riotclient|"
    r"battlenet|roblox-player|minecraft)://",
    re.I,
)


# Launcher clients rather than games. Kept (they are how some games start) but flagged so
# the UI can group them apart instead of mixing them into the library.
_LAUNCHERS = re.compile(
    r"^(battle\.net|epic games launcher|loading bay|riot client|curseforge|"
    r"rockstar games launcher|ea app|origin|ubisoft connect|uplay|gog galaxy|"
    r"steam|xbox|minecraft launcher)$",
    re.I,
)

_STEAM_RUNGAME = re.compile(r"^steam://(?:rungameid|run)/(\d+)", re.I)


def scan(cfg, claimed_paths=None, claimed_appids=None):
    claimed = {p.lower() for p in (claimed_paths or set()) if p}
    claimed_ids = {str(a) for a in (claimed_appids or set()) if a}
    found = {}

    appdata = (os.path.expandvars("%APPDATA%") if winpath.ON_WINDOWS
               else f"C:\\Users\\{cfg.get('windows_user', 'Mabdu')}\\AppData\\Roaming")

    for menu in _START_MENUS:
        root = winpath.native(menu.replace("%APPDATA%", appdata))
        if not os.path.isdir(root):
            continue
        for cur, _dirs, files in os.walk(root):
            for fname in files:
                low = fname.lower()
                if low.endswith(".url"):
                    rec = _from_url(os.path.join(cur, fname))
                elif low.endswith(".lnk"):
                    rec = {"kind": "lnk", "file": os.path.join(cur, fname),
                           "name": fname[:-4]}
                else:
                    continue
                if rec and rec["name"].strip() and not _DENY.search(rec["name"]):
                    found.setdefault(rec["name"].lower(), rec)

    lnks = [r for r in found.values() if r.get("kind") == "lnk"]
    targets = _resolve_lnk_targets([r["file"] for r in lnks])
    for rec in lnks:
        rec["target"] = targets.get(rec["file"].lower())

    games = []
    for rec in found.values():
        record = _to_game(rec, claimed, claimed_ids)
        if record:
            games.append(record)
    return games


def _from_url(path):
    """A .url is INI-ish plain text; no COM needed."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"^URL=(.+)$", text, re.M)
    if not m:
        return None
    return {"kind": "url", "file": path, "name": os.path.basename(path)[:-4],
            "target": m.group(1).strip()}


def _resolve_lnk_targets(paths):
    """Resolve many .lnk targets in one PowerShell call (COM startup dominates cost)."""
    if not paths:
        return {}
    listed = ",".join("'" + winpath.windows(p).replace("'", "''") + "'" for p in paths)
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"@({listed}) | ForEach-Object {{ "
        "  try { $t = $s.CreateShortcut($_).TargetPath } catch { $t = '' } "
        "  [PSCustomObject]@{ Link = $_; Target = $t } }"
    )
    out = {}
    for row in winshell.run_json(script, timeout=90):
        if isinstance(row, dict) and row.get("Link"):
            out[winpath.native(row["Link"]).lower()] = row.get("Target") or ""
            out[row["Link"].lower()] = row.get("Target") or ""
    return out


def _to_game(rec, claimed, claimed_appids):
    target = rec.get("target") or ""
    if not target:
        return None

    is_url = rec["kind"] == "url"
    if is_url:
        if not _GAME_URL.match(target):
            return None
        # Steam writes a .url per *owned* game. If it is installed the Steam scanner
        # already has it with far better metadata; if it is not installed it is out of
        # scope for a dashboard of what is on disk. Either way, drop it.
        m = _STEAM_RUNGAME.match(target)
        if m:
            if m.group(1) not in claimed_appids:
                print(f"[shortcuts] {rec['name']}: owned but not installed; skipping")
            return None
    else:
        if not target.lower().endswith(".exe"):
            return None
        if target.lower() in claimed or not _GAME_LOCATIONS.search(target):
            return None
        # Catch e.g. "Roblox Studio" -> RobloxStudioInstaller.exe.
        if _DENY.search(winpath.basename(target)):
            return None

    ident = re.sub(r"[^a-z0-9]+", "-", rec["name"].lower()).strip("-")
    if not ident:
        return None
    return {
        "is_launcher": bool(_LAUNCHERS.match(rec["name"].strip())),
        "id": f"shortcut:{ident}",
        "name": rec["name"],
        "source": SOURCE,
        "install_dir": None if is_url else winpath.dirname(target),
        "launch": {"kind": "url" if is_url else "exe",
                   "value": target if is_url else winpath.windows(target)},
        "exe_name": None if is_url else winpath.basename(target),
        "exe_path": None if is_url else winpath.windows(target),
        "size_bytes": 0,
        "last_played": None,
        "steam_appid": None,
    }
