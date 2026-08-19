"""Best-effort discovery of where this machine keeps its games.

The scanners know how to read a launcher once they are told where it lives; this module
works out the "where" so a fresh install does not start with an empty grid and a config
file the user is expected to write by hand.

Nothing here is authoritative. It proposes; the user confirms in the settings panel and
the answer is written to config.json. That is why the folder counts come from
`exefind.quick_probe` rather than `exefind.pick`: a wrong count costs one unticked
checkbox, while running the real heuristic across every folder on every drive takes
minutes (each miss is re-walked at depth 6).

Never raises. Every probe degrades to "" / [] / False the way the scanners do.
"""

import os
import string
import time

import config
import exefind
import winpath
import winshell
from scanners import steam

# Whole-run and per-candidate wall-clock budgets. Detection sits in front of a modal, so
# a slow or sleeping drive must cost a partial answer, never a hang.
BUDGET = 8.0
CANDIDATE_BUDGET = 1.2
SAMPLE_LIMIT = 40

# Folder names that commonly hold one subfolder per game.
_COMMON_NAMES = ["Games", "Game", "My Games", "SteamLibrary", "GOG Games",
                 "Games Library", "Program Files (x86)/Steam/steamapps/common"]

# Launcher roots to probe, in order, per drive.
_STEAM_NAMES = ["Program Files (x86)\\Steam", "Program Files\\Steam", "Steam"]


def _ok(path):
    return bool(path) and winpath.isdir(path)


def drives():
    """Every drive letter that currently resolves, most-likely-first."""
    try:
        if winpath.ON_WINDOWS:
            return [f"{c}:\\" for c in string.ascii_uppercase
                    if os.path.exists(f"{c}:\\")]
        return [f"{d.upper()}:\\" for d in sorted(os.listdir("/mnt"))
                if len(d) == 1 and d.isalpha()]
    except OSError:
        return ["C:\\"]


def steam_root():
    """Steam's install root: the registry first, then the usual places.

    Worth the PowerShell call — scanners/steam.py finds *secondary* libraries from
    libraryfolders.vdf, but only once it has the primary root, so a Steam installed off
    C: otherwise loses every Steam game.
    """
    for hive, key in (("HKCU:\\Software\\Valve\\Steam", "SteamPath"),
                      ("HKLM:\\SOFTWARE\\WOW6432Node\\Valve\\Steam", "InstallPath")):
        try:
            out = winshell.run(
                f"(Get-ItemProperty '{hive}' -ErrorAction SilentlyContinue).{key}",
                timeout=20).strip()
        except Exception:
            out = ""
        if out:
            path = winpath.join(out.splitlines()[0].strip())
            if _ok(path):
                return path, "registry"

    for drive in drives():
        for name in _STEAM_NAMES:
            path = winpath.join(drive, name)
            if _ok(path):
                return path, "probe"
    return "", "missing"


def _first_dir(paths):
    for path in paths:
        if _ok(path):
            return path
    return ""


def epic_manifests():
    return _first_dir([
        winpath.join(os.environ.get("ProgramData") or "C:\\ProgramData",
                     "Epic", "EpicGamesLauncher", "Data", "Manifests"),
        "C:\\ProgramData\\Epic\\EpicGamesLauncher\\Data\\Manifests",
    ])


def xbox_games():
    return _first_dir([winpath.join(d, "XboxGames") for d in drives()])


def riot_root():
    return _first_dir([winpath.join(d, "Riot Games") for d in drives()])


def windows_user():
    try:
        return config.windows_user(config.load())
    except Exception:
        return ""


def _count_games(root, ignore, deadline):
    """(count, sampled) for the immediate children of `root` that look like games."""
    kids = [d for d in winpath.listdir(root)
            if d.lower() not in ignore and not d.startswith("$")]
    hits, sampled = 0, False
    for i, name in enumerate(kids):
        if i >= SAMPLE_LIMIT or time.monotonic() > deadline:
            sampled = True
            break
        if exefind.quick_probe(winpath.join(root, name)):
            hits += 1
    return hits, sampled


def candidates(cfg, libraries, deadline):
    """Directories worth offering as scan roots, best first."""
    ignore = {n.lower() for n in cfg.get("ignore_dirs", [])}
    configured = {p.lower().rstrip("\\/")
                  for p in (cfg.get("scan_roots") or []) + (cfg.get("extra_game_dirs") or [])}

    # Whichever drive Windows itself is on. Its root is full of Program Files-style
    # trees that trip the probe without holding a single game, so it is never offered.
    system_drive = (os.environ.get("SystemDrive") or "C:").rstrip("\\/").upper()

    seen, out = set(), []
    probes = []
    for drive in drives():
        if drive.rstrip("\\/").upper() != system_drive:
            probes.append((drive, "drive-root"))
        probes.extend((winpath.join(drive, n.replace("/", "\\")), "probe")
                      for n in _COMMON_NAMES)
    probes.extend((winpath.join(lib, "steamapps", "common"), "steam") for lib in libraries)

    for path, source in probes:
        key = path.lower().rstrip("\\/")
        if key in seen or not _ok(path):
            continue
        seen.add(key)
        if time.monotonic() > deadline:
            break

        # A Steam library root holds one folder — steamapps — which is offered on its
        # own below. Listing both invites ticking the parent and walking it twice.
        if source == "probe" and winpath.isdir(winpath.join(path, "steamapps")):
            continue

        hits, sampled = _count_games(path, ignore, min(time.monotonic() + CANDIDATE_BUDGET,
                                                       deadline))
        # A drive root is mostly system junk, so it has to look convincing to be listed;
        # a folder someone named "Games" only needs one hit.
        if hits < (3 if source == "drive-root" else 1):
            continue
        out.append({
            "path": path,
            "source": source,
            "game_count": hits,
            "sampled": sampled,
            "already_configured": key in configured,
        })

    out.sort(key=lambda c: (-c["game_count"], c["path"]))
    return out


def run(cfg=None, budget=BUDGET):
    """Everything the settings panel needs to propose a working configuration."""
    started = time.monotonic()
    deadline = started + budget
    cfg = cfg or config.load()

    try:
        root, source = steam_root()
    except Exception:
        root, source = "", "missing"

    libraries = []
    if root:
        try:
            libraries = steam.library_paths({**cfg, "steam_root": root})
        except Exception:
            libraries = [root]

    try:
        found = candidates(cfg, libraries, deadline)
    except Exception as exc:
        print(f"[detect] candidate scan failed: {exc}")
        found = []

    return {
        "generated": int(time.time()),
        "elapsed": round(time.monotonic() - started, 2),
        "truncated": time.monotonic() > deadline,
        "drives": drives(),
        "windows_user": windows_user(),
        "launchers": {
            "steam_root": {"path": root, "found": bool(root), "source": source},
            "epic_manifests": {"path": epic_manifests(), "found": bool(epic_manifests())},
            "xbox_games": {"path": xbox_games(), "found": bool(xbox_games())},
            "riot_root": {"path": riot_root(), "found": bool(riot_root())},
        },
        "steam_libraries": libraries,
        "candidates": found,
    }


def main():
    config.safe_console()
    result = run()

    print(f"\nDrives: {' '.join(result['drives'])}")
    print("\nLaunchers")
    for name, info in result["launchers"].items():
        mark = "ok " if info["found"] else "-- "
        print(f"  {mark}{name:16s} {info['path'] or 'not found'}")

    if result["steam_libraries"]:
        print("\nSteam libraries")
        for lib in result["steam_libraries"]:
            print(f"     {lib}")

    print("\nCandidate game folders")
    if not result["candidates"]:
        print("     none found — add yours in Settings, or set scan_roots in config.json")
    for cand in result["candidates"]:
        star = "*" if cand["already_configured"] else " "
        count = f"{cand['game_count']}{'+' if cand['sampled'] else ''}"
        print(f"   {star} {cand['path']:44s} {count:>5s} games   ({cand['source']})")

    print(f"\n{result['elapsed']}s"
          f"{'  (budget reached — partial)' if result['truncated'] else ''}"
          "\n* = already in your config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
