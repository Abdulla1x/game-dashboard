"""Run every scanner, merge, apply overrides, write data/library.json."""

import argparse
import os
import re
import sys
import time

import config
import winpath
from scanners import epic, folders, riot, shortcuts, steam, xbox

# shortcuts runs last: it only fills gaps the others left.
SCANNERS = [
    ("steam", steam),
    ("epic", epic),
    ("xbox", xbox),
    ("riot", riot),
    ("folders", folders),
]


def collect(cfg, verbose=False):
    games, claimed_dirs, claimed_exes = [], [], set()

    def claim(record):
        if record.get("install_dir"):
            claimed_dirs.append(record["install_dir"].lower().rstrip("\\"))
        if record.get("exe_path"):
            claimed_exes.add(record["exe_path"].lower())

    for label, module in SCANNERS:
        start = time.time()
        try:
            found = module.scan(cfg)
        except Exception as exc:  # one broken source must not kill the scan
            print(f"[scan] {label} failed: {exc}")
            found = []
        kept = []
        for record in found:
            if _is_duplicate(record, claimed_dirs, claimed_exes):
                if verbose:
                    print(f"[scan]   dedup {record['name']} ({label})")
                continue
            claim(record)
            kept.append(record)
        games.extend(kept)
        print(f"[scan] {label:9s} {len(kept):3d} games  ({time.time() - start:.1f}s)")

    start = time.time()
    appids = {g["steam_appid"] for g in games if g.get("steam_appid")}
    try:
        found = shortcuts.scan(cfg, claimed_paths=claimed_exes, claimed_appids=appids)
    except Exception as exc:
        print(f"[scan] shortcuts failed: {exc}")
        found = []
    kept = [r for r in found if not _is_duplicate(r, claimed_dirs, claimed_exes)]
    for record in kept:
        claim(record)
    games.extend(kept)
    print(f"[scan] {'shortcuts':9s} {len(kept):3d} games  ({time.time() - start:.1f}s)")

    return games


def _is_duplicate(record, claimed_dirs, claimed_exes):
    exe = (record.get("exe_path") or "").lower()
    if exe and exe in claimed_exes:
        return True
    install = (record.get("install_dir") or "").lower().rstrip("\\")
    if not install:
        return False

    # Containment has to be checked both ways. A launcher's install dir can sit *below*
    # the folder a directory scan finds: Epic installs Among Us to E:\Games\AmongUs\AmongUs,
    # while the folder scanner sees E:\Games\AmongUs. Without the second test the same
    # game is listed twice, once with a good launcher URL and once as a raw exe.
    deep_enough = install.count("\\") >= 2  # never let a drive root swallow the library
    for other in claimed_dirs:
        if install == other or install.startswith(other + "\\"):
            return True
        if deep_enough and other.startswith(install + "\\"):
            return True
    return False


# Lower wins when the same game turns up from two sources. A launcher-backed record
# always beats a bare folder: it carries proper metadata and starts the game the way the
# game expects (Epic's Fortnite at D:\Fortnite over a stale copy in E:\Games\Fortnite).
_SOURCE_RANK = {"steam": 0, "epic": 1, "xbox": 2, "riot": 3, "manual": 4,
                "shortcut": 5, "folder": 6}

_NAME_KEY = re.compile(r"[^a-z0-9]+")


def dedupe_by_name(games, verbose=False):
    """Collapse records that are plainly the same game seen from two sources."""
    best = {}
    for game in games:
        key = _NAME_KEY.sub("", game["name"].lower())
        if not key:
            key = game["id"]
        current = best.get(key)
        if current is None:
            best[key] = game
            continue
        if _SOURCE_RANK.get(game["source"], 9) < _SOURCE_RANK.get(current["source"], 9):
            best[key] = game
            loser = current
        else:
            loser = game
        if verbose:
            print(f"[scan]   name-dedup {loser['name']} ({loser['source']}) "
                  f"-> kept {best[key]['source']}")
    return list(best.values())


def apply_overrides(games, overrides):
    out = []
    by_id = {g["id"]: g for g in games}

    for extra in overrides.get("extra_games", []):
        if extra.get("id") and extra["id"] not in by_id:
            record = _blank_record(extra)
            games.append(record)
            by_id[record["id"]] = record

    for game in games:
        rule = overrides.get(game["id"])
        if not rule:
            out.append(game)
            continue
        if rule.get("hidden"):
            game["hidden"] = True
        if rule.get("name"):
            game["name"] = rule["name"]
        if rule.get("steam_appid"):
            game["steam_appid"] = int(rule["steam_appid"])
        if rule.get("art"):
            game["art_override"] = rule["art"]
        if rule.get("exe"):
            exe = rule["exe"]
            if not winpath.drive_of(exe) and game.get("install_dir"):
                exe = winpath.join(game["install_dir"], exe)
            game["launch"] = {"kind": "exe", "value": exe}
            game["exe_path"] = exe
            game["exe_name"] = winpath.basename(exe)
        out.append(game)
    return out


def _blank_record(extra):
    exe = extra.get("exe")
    return {
        "id": extra["id"],
        "name": extra.get("name") or extra["id"],
        "source": extra.get("source", "manual"),
        "install_dir": extra.get("install_dir") or (winpath.dirname(exe) if exe else None),
        "launch": {"kind": "exe", "value": exe} if exe else {"kind": "none", "value": None},
        "exe_name": winpath.basename(exe) if exe else None,
        "exe_path": exe,
        "size_bytes": 0,
        "last_played": None,
        "steam_appid": extra.get("steam_appid"),
    }


def run(verbose=False, write=True):
    config.ensure_dirs()
    cfg = config.load()
    overrides = config.read_json(config.OVERRIDES_JSON, {})

    games = dedupe_by_name(collect(cfg, verbose), verbose)
    games = apply_overrides(games, overrides)
    games.sort(key=lambda g: g["name"].lower())

    payload = {"generated": int(time.time()), "count": len(games), "games": games}
    if write:
        config.write_json(config.LIBRARY_JSON, payload)
    return payload


def _print_table(games):
    by_source = {}
    for g in games:
        by_source.setdefault(g["source"], []).append(g)

    for source in sorted(by_source):
        rows = sorted(by_source[source], key=lambda g: g["name"].lower())
        print(f"\n=== {source}  ({len(rows)}) " + "=" * 40)
        for g in rows:
            flag = "H" if g.get("hidden") else " "
            size = f"{g['size_bytes'] / 2**30:6.1f}G" if g.get("size_bytes") else "      -"
            print(f" {flag} {g['name'][:38]:40s} {size}  {g.get('exe_name') or g['launch']['kind']}")


def _selftest(games):
    """Encodes the plan's verification criteria."""
    by_id = {g["id"]: g for g in games}
    names = {g["name"].lower() for g in games}
    failures = []

    # Decoys: clip folders on G:\ that contain no executable.
    for decoy in ("folder:GVALORANT", "folder:GCounter-Strike 2",
                  "folder:GKovaaK's", "folder:Grocketleague"):
        if decoy in by_id:
            failures.append(f"decoy folder was listed as a game: {decoy}")

    stray = by_id.get("folder:EGames--Stray")
    if stray and stray.get("exe_name", "").lower().startswith("unins"):
        failures.append("Stray resolved to the uninstaller")

    steam_games = [g for g in games if g["source"] == "steam"]
    if len(steam_games) < 15:
        failures.append(f"expected >=15 Steam games, got {len(steam_games)}")

    seen_names = {}
    for g in games:
        key = _NAME_KEY.sub("", g["name"].lower())
        if key in seen_names:
            failures.append(
                f"duplicate game: {g['name']} ({g['source']}) and "
                f"{seen_names[key]['name']} ({seen_names[key]['source']})")
        seen_names[key] = g

    for g in games:
        if not g.get("id") or not g.get("name"):
            failures.append(f"record missing id/name: {g}")
        if g["launch"]["kind"] != "none" and not g["launch"].get("value"):
            failures.append(f"{g['name']}: launch kind {g['launch']['kind']} has no value")

    print("\n" + "=" * 60)
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        print(f"\n{len(failures)} check(s) failed")
        return 1
    print(f"PASS  {len(games)} games, all checks green")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Scan the game library.")
    ap.add_argument("--dry-run", action="store_true", help="print results, do not write")
    ap.add_argument("--selftest", action="store_true", help="assert verification criteria")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    payload = run(verbose=args.verbose, write=not (args.dry_run or args.selftest))
    games = payload["games"]

    _print_table(games)
    visible = [g for g in games if not g.get("hidden")]
    print(f"\nTotal: {len(games)} ({len(visible)} visible)")

    if args.selftest:
        return _selftest(games)
    if not args.dry_run:
        print(f"Wrote {config.LIBRARY_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
