"""Pick the executable that actually launches a game, given its install folder.

This is the riskiest heuristic in the project: repack folders bury the real binary
among redistributables, uninstallers and crash handlers, sometimes two or three
levels deep. Validated against the real library — see `--selftest` in scan.py.
"""

import difflib
import os
import re

import winpath

MAX_DEPTH = 4
# Retry depth for folders that yield nothing at MAX_DEPTH. Unreal-style repacks bury the
# binary at `<Repack>/<Game>/Gameface/Binaries/Win64/Foo.exe` — depth 5. Scanning this deep
# for every folder would be slow and noisy, so it is only a fallback.
DEEP_DEPTH = 6

# Directories whose executables are never the game.
_SKIP_DIRS = {
    "_redist", "_commonredist", "commonredist", "redist", "redistributables",
    "__installer", "installer", "directx", "direct x", "dotnet", "dotnetfx",
    "vcredist", "_commonredist", "prereq", "prerequisites", "support",
    "extras", "engine", "dxsetup", "_installer", "backup", "docs",
}

# Filename patterns that are never the game (matched against the lowercased stem).
_SKIP_PATTERNS = [
    r"^unins", r"uninstall", r"crashhandler", r"crashreport", r"^vcredist",
    r"^vc_redist", r"^dxwebsetup", r"^dxsetup", r"^oalinst", r"^dotnetfx",
    r"^quicksfv", r"^cleanup$", r"^touchup$", r"setup$", r"^setup",
    r"^install", r"install$", r"notification_helper", r"^epicwebhelper",
    r"unrealcefsubprocess", r"^subprocess", r"^7z", r"^winrar", r"^config$",
    r"^launcher_", r"helper$", r"^crashpad", r"^bsdiff", r"^python",
    r"^dxdiag", r"^easyanticheat", r"^battleye", r"^beservice", r"^activation",
    # Shipped developer/console tools that sit beside the real binary.
    r"^vconsole", r"^hammer", r"^workshop", r"^resourcecompiler", r"^shadercompile",
    r"^meshsystem", r"^demoinfo", r"^steamerrorreporter", r"^gameoverlayui",
    r"^benchmark", r"^editor$", r"^server$", r"^dedicated",
]
_SKIP_RE = [re.compile(p) for p in _SKIP_PATTERNS]

# Weak-but-not-disqualifying names: used only if nothing better exists.
_WEAK = {"launcher", "start", "play", "game", "run", "loader"}

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text):
    return _NON_ALNUM.sub("", (text or "").lower())


def _is_rejected(stem):
    low = stem.lower()
    return any(rx.search(low) for rx in _SKIP_RE)


def _depth(path):
    """Separator count, whichever separator this platform's paths actually use.

    os.walk hands back native paths: forward slashes under WSL, backslashes on Windows.
    Counting only "/" made every Windows path measure as depth 0, so the max_depth prune
    below never fired and each game folder was walked to the bottom — minutes per scan,
    and depth scoring that always saw 0.
    """
    return path.replace("\\", "/").rstrip("/").count("/")


def _walk(dir_win, max_depth):
    """Yield (win_path, depth, size) for every .exe under dir_win, pruning junk dirs."""
    root_native = winpath.native(dir_win)
    if not os.path.isdir(root_native):
        return

    base_depth = _depth(root_native)
    for cur, subdirs, files in os.walk(root_native):
        depth = _depth(cur) - base_depth
        if depth >= max_depth:
            subdirs[:] = []
        else:
            subdirs[:] = [d for d in subdirs if d.lower() not in _SKIP_DIRS]

        for fname in files:
            if not fname.lower().endswith(".exe"):
                continue
            full = os.path.join(cur, fname)
            try:
                fsize = os.path.getsize(full)
            except OSError:
                fsize = 0
            yield winpath.windows(full), depth, fsize


# Entries examined before a probe gives up and answers from what it has seen. Detection
# runs this across every folder on every drive, so it is a budget, not a limit.
PROBE_DEPTH = 2
PROBE_ENTRY_CAP = 400


def quick_probe(dir_win, max_depth=PROBE_DEPTH, entry_cap=PROBE_ENTRY_CAP):
    """Cheap yes/no: does this folder look like it holds a game?

    Stops at the first plausible .exe, at max_depth, or after entry_cap files — and
    never triggers the depth-6 retry, which is what makes `pick` too slow to run across
    a whole drive just to count candidates. `pick` remains the authority on what a game
    actually is; this only decides what to *offer* during setup, so a false positive
    costs the user one unticked checkbox.
    """
    root_native = winpath.native(dir_win)
    if not os.path.isdir(root_native):
        return False

    seen = 0
    base_depth = _depth(root_native)
    try:
        for cur, subdirs, files in os.walk(root_native):
            if _depth(cur) - base_depth >= max_depth:
                subdirs[:] = []
            else:
                subdirs[:] = [d for d in subdirs if d.lower() not in _SKIP_DIRS]

            for fname in files:
                seen += 1
                if seen > entry_cap:
                    return False
                if fname.lower().endswith(".exe") and not _is_rejected(fname[:-4]):
                    return True
    except OSError:
        return False
    return False


def candidates(dir_win, game_name=None, max_depth=MAX_DEPTH, limit=25, deep=True):
    """All plausible launch executables, best first.

    Returns a list of dicts: {path, name, depth, size, score}. When `deep` is set and the
    normal-depth pass finds nothing, retries at DEEP_DEPTH before giving up.
    """
    found = _scan(dir_win, game_name, max_depth)
    if not found and deep and max_depth < DEEP_DEPTH:
        found = _scan(dir_win, game_name, DEEP_DEPTH)
    return found[:limit]


def _scan(dir_win, game_name, max_depth):
    target = _norm(game_name or winpath.basename(dir_win))
    found = []

    for path, depth, fsize in _walk(dir_win, max_depth):
        stem = winpath.basename(path)[:-4]
        if _is_rejected(stem):
            continue

        norm_stem = _norm(stem)
        if not norm_stem:
            continue

        # Name similarity dominates: the binary usually echoes the folder name.
        ratio = difflib.SequenceMatcher(None, norm_stem, target).ratio()
        if target and (norm_stem in target or target in norm_stem):
            ratio = max(ratio, 0.85)
        score = ratio * 100

        score -= depth * 6                      # prefer shallow
        score += min(fsize / (1024 * 1024), 60) / 6   # prefer substantial binaries
        if re.search(r"(x64|win64|64)$", norm_stem):
            score += 4
        if re.search(r"(x86|win32|32)$", norm_stem):
            score -= 4
        if norm_stem in _WEAK:
            score -= 25                          # bare "Launcher.exe" and friends
        if fsize < 64 * 1024:
            score -= 15                          # stubs and shims

        found.append({
            "path": path,
            "name": winpath.basename(path),
            "depth": depth,
            "size": fsize,
            "score": round(score, 2),
        })

    found.sort(key=lambda c: (-c["score"], c["depth"], -c["size"]))
    return found


def pick(dir_win, game_name=None, max_depth=MAX_DEPTH):
    """Best launch executable for a folder, or None if it holds no game."""
    found = candidates(dir_win, game_name, max_depth, limit=1)
    return found[0] if found else None
