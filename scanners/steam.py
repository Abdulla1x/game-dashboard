"""Steam scanner: libraryfolders.vdf -> per-library appmanifest_*.acf."""

import os
import re

import exefind
import vdf
import winpath

SOURCE = "steam"

# Runtimes and redistributables that ship as "apps" but are not games.
_SKIP_APPIDS = {
    "228980",   # Steamworks Common Redistributables
    "1070560",  # Steam Linux Runtime 1.0 (scout)
    "1391110",  # Steam Linux Runtime 2.0 (soldier)
    "1628350",  # Steam Linux Runtime 3.0 (sniper)
    "1493710",  # Proton Experimental
    "2180100",  # Proton Hotfix
}
_SKIP_NAME = re.compile(
    r"^(steamworks|steam linux runtime|proton|steam controller|steamvr)", re.I
)

_MANIFEST_RE = re.compile(r"^appmanifest_(\d+)\.acf$", re.I)


def library_paths(cfg):
    """Every Steam library root, from libraryfolders.vdf (plus the install root)."""
    steam_root = cfg["steam_root"]
    roots = [steam_root]

    lib_file = winpath.join(steam_root, "steamapps", "libraryfolders.vdf")
    if winpath.exists(lib_file):
        try:
            data = vdf.load(winpath.native(lib_file))
        except OSError as exc:
            print(f"[steam] cannot read libraryfolders.vdf: {exc}")
            data = {}

        folders = vdf.get(data, "libraryfolders", default={}) or {}
        for entry in folders.values():
            path = vdf.get(entry, "path") if isinstance(entry, dict) else None
            if path:
                roots.append(path)

    seen, unique = set(), []
    for r in roots:
        key = r.lower().rstrip("\\/")
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def scan(cfg):
    games = []
    for lib in library_paths(cfg):
        steamapps = winpath.join(lib, "steamapps")
        native = winpath.native(steamapps)
        if not os.path.isdir(native):
            continue

        for fname in sorted(os.listdir(native)):
            if not _MANIFEST_RE.match(fname):
                continue
            record = _read_manifest(os.path.join(native, fname), steamapps)
            if record:
                games.append(record)
    return games


def _read_manifest(native_path, steamapps_win):
    try:
        data = vdf.load(native_path)
    except OSError as exc:
        print(f"[steam] skipping {native_path}: {exc}")
        return None

    state = vdf.get(data, "AppState", default={}) or {}
    appid = vdf.get(state, "appid")
    name = vdf.get(state, "name")
    installdir = vdf.get(state, "installdir")

    if not appid or not installdir:
        return None
    if appid in _SKIP_APPIDS or (name and _SKIP_NAME.match(name)):
        return None

    install_dir = winpath.join(steamapps_win, "common", installdir)
    if not winpath.isdir(install_dir):
        return None  # manifest left behind after an uninstall

    # Shallow, non-deep: we only need a process name for playtime tracking, and a
    # full deep walk across every Steam game would dominate scan time.
    exe = exefind.pick(install_dir, name, max_depth=3)

    return {
        "id": f"steam:{appid}",
        "name": name or installdir,
        "source": SOURCE,
        "install_dir": install_dir,
        "launch": {"kind": "url", "value": f"steam://rungameid/{appid}"},
        "exe_name": exe["name"] if exe else None,
        "exe_path": exe["path"] if exe else None,
        "size_bytes": _int(vdf.get(state, "SizeOnDisk")),
        "last_played": _int(vdf.get(state, "LastPlayed")) or None,
        "steam_appid": int(appid),
    }


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
