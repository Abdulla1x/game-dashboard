"""Standalone / repack game folders — the messiest and largest source.

A directory becomes a game only if `exefind` finds a plausible executable in it. That
gate is load-bearing rather than a nicety: clip recorders (Medal, ReLive, ShadowPlay)
create a folder named after every game you play, sitting right beside real installs and
containing nothing but video. Drop the gate and each one becomes a phantom game that
cannot be launched.
"""

import os

import config
import exefind
import winpath

SOURCE = "folder"


def folder_id(dir_win):
    return "folder:" + dir_win.replace(":", "").replace("/", "\\").strip("\\").replace("\\", "--")


def scan(cfg):
    ignore = {name.lower() for name in cfg.get("ignore_dirs", [])}
    sizes = config.read_json(os.path.join(config.DATA_DIR, "sizes.json"), {})

    candidates = []
    for root in cfg.get("scan_roots", []):
        for entry in winpath.listdir(root):
            if entry.lower() in ignore or entry.startswith("$"):
                continue
            full = winpath.join(root, entry)
            if winpath.isdir(full):
                candidates.append(full)

    for extra in cfg.get("extra_game_dirs", []):
        if winpath.isdir(extra):
            candidates.append(extra)

    games = []
    seen = set()
    for dir_win in candidates:
        key = dir_win.lower()
        if key in seen:
            continue
        seen.add(key)

        name = winpath.basename(dir_win)
        exe = exefind.pick(dir_win, name)
        if not exe:
            continue  # the gate

        games.append({
            "id": folder_id(dir_win),
            "name": name,
            "source": SOURCE,
            "install_dir": dir_win,
            "launch": {"kind": "exe", "value": exe["path"]},
            "exe_name": exe["name"],
            "exe_path": exe["path"],
            "size_bytes": sizes.get(dir_win, 0),
            "last_played": None,
            "steam_appid": None,
        })
    return games
