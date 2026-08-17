"""Cover art resolution.

Order: local Steam cache -> Steam CDN by appid -> Steam CDN via fuzzy name match ->
extracted exe icon -> generated placeholder. Each hit is cached under data/art/.

Results carry a `kind` so the UI can style them differently: a real 600x900 `cover`
is displayed edge to edge, while a 32px `icon` is centered on a tinted card rather
than stretched into a blurry mess.
"""

import difflib
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import winpath
import winshell

_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
_CDN_ALT = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"

# Steam's community search: a targeted name -> appid lookup that needs no API key and
# no 10 MB bulk download, and applies Steam's own fuzzy matching ("Elden Ring" finds
# "ELDEN RING"). storesearch is a second opinion when the first returns nothing.
_SEARCH_URL = "https://steamcommunity.com/actions/SearchApps/{term}"
_STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/?term={term}&cc=us&l=en"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; game-dashboard/1.0)"}

# A returned title must look like what we asked for; Steam search always returns
# *something*, and an unmatched query would otherwise pick up a random game's art.
_MATCH_CUTOFF = 0.72

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Edition/version noise that stops an otherwise exact name match.
_NOISE = re.compile(
    r"\b(definitive|complete|deluxe|ultimate|goty|game of the year|remastered|"
    r"anniversary|enhanced|legacy|edition|season|repack|multiplayer|"
    r"v?\d+\.\d[\d.]*)\b",
    re.I,
)

_cache_lock = threading.Lock()
_appid_cache = None


def _norm(text):
    return _NON_ALNUM.sub("", (text or "").lower())


def _simplify(name):
    return _norm(_NOISE.sub(" ", name or ""))


# -- Steam app list ------------------------------------------------------


def _cache():
    """Normalized name -> appid (0 means "searched, no good match")."""
    global _appid_cache
    if _appid_cache is None:
        _appid_cache = config.read_json(config.APPLIST_JSON, {})
    return _appid_cache


def _fetch_json(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[art] search failed ({exc}) for {url[:70]}")
        return None


def _similar(query, candidate):
    a, b = _simplify(query), _simplify(candidate)
    if not a or not b:
        return 0.0
    # Containment alone is not enough: "God of War" is contained in "God of War
    # Ragnarok", and treating that as a perfect match picks the wrong game's art.
    # Only count it when the two are close in length.
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ABBREV = [
    (re.compile(r"\bgta\b", re.I), "Grand Theft Auto"),
    (re.compile(r"\bnfs\b", re.I), "Need for Speed"),
    (re.compile(r"\bcod\b", re.I), "Call of Duty"),
    (re.compile(r"\brdr\s*2\b", re.I), "Red Dead Redemption 2"),
]


def _query_variants(name):
    """Progressively looser search terms; first confident hit wins."""
    variants = [name]

    # "FallGuys" -> "Fall Guys", so Steam's tokenizer can match it.
    split = _CAMEL.sub(" ", name)
    if split != name:
        variants.append(split)

    expanded = split
    for pattern, full in _ABBREV:
        expanded = pattern.sub(full, expanded)
    if expanded != split:
        variants.append(expanded)

    # Drop edition/version noise: "Skyrim Anniversary Edition" -> "Skyrim".
    stripped = _NOISE.sub(" ", expanded)
    stripped = re.sub(r"[-–—:]+", " ", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped.lower() != expanded.lower() and len(stripped) >= 3:
        variants.append(stripped)

    seen, out = set(), []
    for v in variants:
        key = v.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(v.strip())
    return out


def _search_once(query, score_against):
    term = urllib.parse.quote(query)

    results = _fetch_json(_SEARCH_URL.format(term=term)) or []
    ranked = [(r.get("name"), r.get("appid")) for r in results
              if isinstance(r, dict) and r.get("appid")]

    if not ranked:
        store = _fetch_json(_STORE_SEARCH_URL.format(term=term)) or {}
        ranked = [(i.get("name"), i.get("id")) for i in store.get("items", [])
                  if isinstance(i, dict) and i.get("id")]

    best, best_score = None, 0.0
    for title, appid in ranked:
        # Score against the query actually used as well as the original name, so a
        # loosened variant is not penalised for the noise it deliberately dropped.
        score = max(_similar(score_against, title), _similar(query, title))
        if score > best_score:
            best, best_score = (title, appid), score
    return best, best_score


def _search(name):
    """Query Steam for a title, returning an appid only on a confident match."""
    overall, overall_score = None, 0.0

    for query in _query_variants(name):
        best, score = _search_once(query, name)
        if score > overall_score:
            overall, overall_score = best, score
        if best and score >= _MATCH_CUTOFF:
            try:
                return int(best[1])
            except (TypeError, ValueError):
                return None

    if overall:
        print(f"[art] {name!r}: best match {overall[0]!r} scored "
              f"{overall_score:.2f}, below cutoff")
    return None


def guess_appid(name):
    """Best-effort Steam appid for a non-Steam game name. Cached, including misses."""
    key = _norm(name)
    if not key or len(key) < 3:
        return None

    with _cache_lock:
        cache = _cache()
        if key in cache:
            return cache[key] or None

    appid = _search(name)

    with _cache_lock:
        _cache()[key] = appid or 0
        config.write_json(config.APPLIST_JSON, _cache())
    return appid


# -- fetching ------------------------------------------------------------


def _download(url, dest):
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            if resp.status != 200:
                return False
            payload = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        return False
    if len(payload) < 1024:
        return False
    with open(dest, "wb") as fh:
        fh.write(payload)
    return True


# Portrait art first, then landscape as a last local resort. Newer Steam builds name the
# portrait "library_capsule.jpg" where older ones used "library_600x900.jpg".
_LOCAL_ART_NAMES = (
    "library_600x900_2x.jpg",
    "library_600x900.jpg",
    "library_capsule_2x.jpg",
    "library_capsule.jpg",
    "library_header.jpg",
)


def _local_steam_cover(cfg, appid):
    """Find Steam's own cached art for an appid.

    Older Steam wrote these files directly into librarycache/<appid>/. Newer builds
    put each one inside its own hash-named subdirectory, so both layouts are searched
    — without this, installed games fall through to the network and then to a 32px
    exe icon, even though Steam already has the real cover on disk.
    """
    base = winpath.native(
        winpath.join(cfg["steam_root"], "appcache", "librarycache", str(appid))
    )
    if not os.path.isdir(base):
        return None

    found = {}
    try:
        entries = os.listdir(base)
    except OSError:
        return None

    for entry in entries:
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            for name in _LOCAL_ART_NAMES:
                nested = os.path.join(full, name)
                if name not in found and os.path.exists(nested):
                    found[name] = nested
        elif entry in _LOCAL_ART_NAMES:
            found.setdefault(entry, full)

    for name in _LOCAL_ART_NAMES:
        path = found.get(name)
        if path and os.path.getsize(path) > 2048:
            return path
    return None


def _extract_icon(exe_path, dest_png):
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$i=[System.Drawing.Icon]::ExtractAssociatedIcon('{winpath.windows(exe_path)}'); "
        f"$i.ToBitmap().Save('{winpath.windows(dest_png)}',"
        "[System.Drawing.Imaging.ImageFormat]::Png)"
    )
    winshell.run(script, timeout=30)
    return os.path.exists(dest_png) and os.path.getsize(dest_png) > 200


# -- public --------------------------------------------------------------


def resolve(game, cfg):
    """Return (path, kind) for a game's art, or (None, 'placeholder')."""
    config.ensure_dirs()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", game["id"])
    jpg = os.path.join(config.ART_DIR, safe + ".jpg")
    png = os.path.join(config.ART_DIR, safe + ".png")
    miss = os.path.join(config.ART_DIR, safe + ".miss")

    if os.path.exists(jpg):
        return jpg, "cover"
    if os.path.exists(png):
        return png, "icon"

    override = game.get("art_override")
    if override and os.path.exists(winpath.native(override)):
        return winpath.native(override), "cover"

    if os.path.exists(miss) and time.time() - os.path.getmtime(miss) < 86400:
        return None, "placeholder"

    appid = game.get("steam_appid")

    # 1. Steam's own cache.
    if appid:
        local = _local_steam_cover(cfg, appid)
        if local:
            return local, "cover"

    # 2/3. CDN, by known appid or by name match.
    if not appid:
        appid = guess_appid(game["name"])
        if appid:
            game["steam_appid"] = appid  # surfaced in the UI so a bad guess is fixable

    if appid:
        for url in (_CDN.format(appid=appid), _CDN_ALT.format(appid=appid)):
            if _download(url, jpg):
                return jpg, "cover"

    # 4. The executable's own icon.
    exe = game.get("exe_path")
    if exe and winpath.exists(exe) and _extract_icon(exe, png):
        return png, "icon"

    # 5. Nothing worked; remember that for a day so we do not retry on every load.
    open(miss, "w").close()
    return None, "placeholder"


def placeholder_svg(name):
    """Deterministic lettered card, used when every lookup fails."""
    digest = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    hue = int(digest[:4], 16) % 360
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name or "?")[:2]).upper()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 900">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="hsl({hue},38%,32%)"/>'
        f'<stop offset="100%" stop-color="hsl({(hue + 40) % 360},42%,16%)"/>'
        f"</linearGradient></defs>"
        f'<rect width="600" height="900" fill="url(#g)"/>'
        f'<text x="300" y="470" font-family="Segoe UI,Arial,sans-serif" font-size="210" '
        f'font-weight="700" fill="rgba(255,255,255,.82)" text-anchor="middle">'
        f"{initials or '?'}</text></svg>"
    ).encode("utf-8")
