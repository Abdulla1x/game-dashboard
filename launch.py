"""Start a game. One code path per launch kind."""

import os
import subprocess

import winpath

_NO_WINDOW = 0x08000000


class LaunchError(Exception):
    pass


def launch(game):
    """Launch `game`, returning a short human-readable description of what ran."""
    spec = game.get("launch") or {}
    kind, value = spec.get("kind"), spec.get("value")
    if not kind or kind == "none" or not value:
        raise LaunchError(f"{game['name']} has no launch command")

    if kind == "url":
        _open_url(value)
        return value

    if kind == "shell":
        _spawn(["explorer.exe", f"shell:AppsFolder\\{value}"], cwd=None)
        return f"shell:AppsFolder\\{value}"

    if kind in ("exe", "exe_args"):
        native = winpath.native(value)
        if not os.path.exists(native):
            raise LaunchError(f"executable not found: {value}")
        # Working directory is mandatory: many repacked games look for their assets
        # relative to the CWD and fail silently when launched from elsewhere.
        _spawn([native] + list(spec.get("args") or []), cwd=os.path.dirname(native))
        return value

    raise LaunchError(f"unknown launch kind: {kind}")


def _open_url(url):
    if hasattr(os, "startfile"):
        os.startfile(url)  # Windows
    else:
        # WSL: `start` is a cmd builtin, and the empty "" is the required title arg.
        subprocess.Popen(["cmd.exe", "/c", "start", "", url], cwd="/mnt/c",
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _spawn(argv, cwd):
    subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW if winpath.ON_WINDOWS else 0,
    )


def reveal(game):
    """Open Explorer at the game's folder, selecting the executable when known."""
    exe = game.get("exe_path")
    if exe and winpath.exists(exe):
        _spawn(["explorer.exe", f"/select,{winpath.windows(exe)}"], cwd=None)
        return
    target = game.get("install_dir")
    if not target or not winpath.exists(target):
        raise LaunchError(f"{game['name']} has no folder on disk")
    _spawn(["explorer.exe", winpath.windows(target)], cwd=None)
