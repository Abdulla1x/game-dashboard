"""Epic Games Launcher: %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests\\*.item (JSON)."""

import json
import os

import winpath

SOURCE = "epic"


def scan(cfg):
    manifest_dir = cfg["epic_manifests"]
    native = winpath.native(manifest_dir)
    if not os.path.isdir(native):
        return []

    games = []
    for fname in sorted(os.listdir(native)):
        if not fname.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(native, fname), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"[epic] skipping {fname}: {exc}")
            continue

        # Epic writes a manifest per *component*; only real applications are games.
        if not data.get("bIsApplication", True):
            continue

        app_name = data.get("AppName") or data.get("MainGameAppName")
        install = data.get("InstallLocation")
        display = data.get("DisplayName") or data.get("MandatoryAppFolderName")
        launch_exe = data.get("LaunchExecutable")
        if not app_name or not install:
            continue

        exe_path = winpath.join(install, launch_exe) if launch_exe else None

        games.append({
            "id": f"epic:{app_name}",
            "name": display or app_name,
            "source": SOURCE,
            "install_dir": install,
            "launch": {
                "kind": "url",
                "value": (
                    f"com.epicgames.launcher://apps/{app_name}"
                    "?action=launch&silent=true"
                ),
            },
            "exe_name": winpath.basename(launch_exe) if launch_exe else None,
            "exe_path": exe_path,
            "size_bytes": 0,
            "last_played": None,
            "steam_appid": None,
        })
    return games
