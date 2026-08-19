"""Cover art resolution.

Nothing landscape is ever served as-is: cropping a header into a 2:3 tile throws away
two thirds of it. Where a wide banner is all that exists, it gets letterboxed onto a
generated card instead.

Order: local Steam cache -> art the scanner supplied (Epic's own store art) -> Steam CDN
by appid -> Steam CDN via fuzzy name match -> SteamGridDB (needs a key) -> a letterboxed
Steam header -> a card generated from the executable's own icon -> generated placeholder.
Each hit is cached under data/art/.

Results carry a `kind` so the UI can style them differently. Everything except
`placeholder` fills the tile edge to edge: a `cover` is real 2:3 art, while a `card` is
drawn here from the exe's 256px icon on a colour sampled from that icon. Bare `icon` is
only reached when the icon exists but could not be decoded to build a card from.
"""

import base64
import difflib
import hashlib
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import peicon
import winpath
import winshell

_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"

# SteamGridDB carries portrait covers for the many things Steam has no appid for —
# launchers, Epic/Riot exclusives, mod clients. Needs a free key in config.json.
_SGDB_SEARCH = "https://api.steamgriddb.com/api/v2/search/autocomplete/{term}"
_SGDB_BY_STEAM = "https://api.steamgriddb.com/api/v2/games/steam/{appid}"
_SGDB_GRIDS = ("https://api.steamgriddb.com/api/v2/grids/game/{gid}"
               "?dimensions=600x900,342x482&types=static&nsfw=false")

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


# Portrait only. Newer Steam builds name the portrait "library_capsule.jpg" where older
# ones used "library_600x900.jpg". The landscape "library_header.jpg" is deliberately not
# here: at 460x215 in a 2:3 tile it gets cropped to the middle third and loses the title,
# and a card generated from the game's own icon reads better than that.
_LOCAL_ART_NAMES = (
    "library_600x900_2x.jpg",
    "library_600x900.jpg",
    "library_capsule_2x.jpg",
    "library_capsule.jpg",
)

# Landscape. Never served as-is — a 2:3 tile would crop away two thirds of it — but it
# can be letterboxed into a card, which beats a lettered placeholder.
_LOCAL_WIDE_NAMES = ("library_header.jpg", "header.jpg")


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


def _local_steam_wide(cfg, appid):
    """Steam's landscape header for an appid, if that is all it has."""
    base = winpath.native(
        winpath.join(cfg["steam_root"], "appcache", "librarycache", str(appid))
    )
    if not os.path.isdir(base):
        return None
    try:
        entries = os.listdir(base)
    except OSError:
        return None

    for entry in entries:
        full = os.path.join(base, entry)
        if os.path.isdir(full):
            for name in _LOCAL_WIDE_NAMES:
                nested = os.path.join(full, name)
                if os.path.exists(nested) and os.path.getsize(nested) > 2048:
                    return nested
        elif entry in _LOCAL_WIDE_NAMES and os.path.getsize(full) > 2048:
            return full
    return None


def _extract_icon(exe_path, dest_png):
    """Write the executable's icon to `dest_png`, as large as it can be had.

    The PE resource directory usually holds a 256px icon; PowerShell's
    ExtractAssociatedIcon is capped at 32px, so it is only the fallback for binaries
    with no icon resource of their own.
    """
    if peicon.write_largest_icon(exe_path, dest_png):
        return True

    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$i=[System.Drawing.Icon]::ExtractAssociatedIcon('{winpath.windows(exe_path)}'); "
        f"$i.ToBitmap().Save('{winpath.windows(dest_png)}',"
        "[System.Drawing.Imaging.ImageFormat]::Png)"
    )
    winshell.run(script, timeout=30)
    return os.path.exists(dest_png) and os.path.getsize(dest_png) > 200


# -- SteamGridDB ---------------------------------------------------------

_sgdb_offline = False


def reset_network_state():
    """Forget that lookups failed, so a corrected API key takes effect immediately.

    _sgdb_offline disables SteamGridDB for the rest of the run after the first failure,
    which is right when the key is wrong or DNS is broken -- but it also means saving a
    working key in Settings would appear to do nothing until a restart. The .miss
    markers block re-resolution for 24h for the same reason, so they go too.
    """
    global _sgdb_offline
    _sgdb_offline = False

    dropped = 0
    try:
        for name in os.listdir(config.ART_DIR):
            if name.endswith(".miss"):
                try:
                    os.remove(os.path.join(config.ART_DIR, name))
                    dropped += 1
                except OSError:
                    pass
    except OSError:
        return 0
    if dropped:
        print(f"[art] cleared {dropped} cached miss marker(s)")
    return dropped


def _sgdb_get(url, key):
    """GET a SteamGridDB endpoint. Returns the `data` payload, or None."""
    global _sgdb_offline
    if _sgdb_offline:
        return None
    headers = dict(_UA)
    headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            print("[art] SteamGridDB rejected the key; disabling for this run")
            _sgdb_offline = True
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # A DNS failure means the host is unreachable from here; stop retrying it for
        # every remaining game rather than paying the timeout 300 times.
        print(f"[art] SteamGridDB unreachable ({exc}); disabling for this run")
        _sgdb_offline = True
        return None
    return payload.get("data") if isinstance(payload, dict) else None


def _steamgriddb_cover(game, cfg, dest):
    key = (cfg.get("steamgriddb_key") or "").strip()
    if not key:
        return False

    gid = None
    appid = game.get("steam_appid")
    if appid:
        found = _sgdb_get(_SGDB_BY_STEAM.format(appid=appid), key)
        if isinstance(found, dict):
            gid = found.get("id")

    if not gid:
        term = urllib.parse.quote(game["name"])
        results = _sgdb_get(_SGDB_SEARCH.format(term=term), key) or []
        for item in results:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            # Same guard as the Steam name search: an unrelated top hit is worse
            # than no art at all.
            if _similar(game["name"], item.get("name") or "") >= _MATCH_CUTOFF:
                gid = item["id"]
                break

    if not gid:
        return False

    grids = _sgdb_get(_SGDB_GRIDS.format(gid=gid), key) or []
    for grid in grids:
        url = isinstance(grid, dict) and grid.get("url")
        if url and _download(url, dest):
            return True
    return False


def _package_logo(install_dir):
    """The logo a Store/Xbox package ships with, as PNG bytes.

    Xbox titles have no exe of their own to take an icon from — the launch value is an
    AUMID — but every package carries its own tile artwork next to the manifest.
    """
    if not install_dir:
        return None
    root = winpath.native(install_dir)
    best, best_area = None, 0
    for folder in (root, os.path.join(root, "Content")):
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            low = entry.lower()
            if not low.endswith(".png") or "logo" not in low:
                continue
            # Skip the high-contrast accessibility variants; they are flat silhouettes.
            if "contrast-" in low:
                continue
            path = os.path.join(folder, entry)
            try:
                with open(path, "rb") as fh:
                    head = fh.read(24)
                if head[:8] != b"\x89PNG\r\n\x1a\n":
                    continue
                width, height = struct.unpack(">II", head[16:24])
            except (OSError, struct.error):
                continue
            # Tile art is square-ish; a wide splash screen makes a poor centrepiece.
            if not height or not 0.7 <= width / height <= 1.4:
                continue
            if width * height > best_area:
                best_area, best = width * height, path

    if not best or best_area < 64 * 64:
        return None
    try:
        with open(best, "rb") as fh:
            return fh.read()
    except OSError:
        return None


# -- generated cover card ------------------------------------------------


def _dominant_color(png_bytes):
    """A representative colour for an icon: the most common vivid tone in it."""
    decoded = peicon.decode_png_rgba(png_bytes)
    if not decoded:
        return None
    _w, _h, rgba = decoded

    buckets = {}
    fallback = [0, 0, 0, 0]
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        if a < 128:
            continue
        fallback[0] += r
        fallback[1] += g
        fallback[2] += b
        fallback[3] += 1
        hi, lo = max(r, g, b), min(r, g, b)
        # Weight by how much colour a pixel actually carries, so a logo's accent wins
        # over the large flat black or white area around it.
        weight = 1 + (hi - lo) * 3
        if hi < 26 or lo > 235:
            weight = 1
        key = (r >> 4, g >> 4, b >> 4)
        slot = buckets.setdefault(key, [0, 0, 0, 0])
        slot[0] += r * weight
        slot[1] += g * weight
        slot[2] += b * weight
        slot[3] += weight

    if not fallback[3]:
        return None
    if buckets:
        best = max(buckets.values(), key=lambda s: s[3])
        return (best[0] // best[3], best[1] // best[3], best[2] // best[3])
    return (fallback[0] // fallback[3], fallback[1] // fallback[3],
            fallback[2] // fallback[3])


def _shade(rgb, factor, floor=0):
    return tuple(max(floor, min(255, int(c * factor))) for c in rgb)


def _normalize(rgb):
    """Pull a colour into a mid range so the card is neither black nor washed out.

    Plenty of icons are dominated by near-black or near-white — launcher icons and
    minimal logos especially — and shading those directly gives a flat grey card.
    """
    lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    if lum < 1:
        return (58, 62, 74)  # a neutral slate, for a wholly black icon
    if lum < 70:
        return _shade(rgb, 70 / lum)
    if lum > 190:
        return _shade(rgb, 190 / lum)
    return rgb


def _card_svg(png_bytes):
    """A full-bleed 2:3 cover built around an icon, tinted to match it.

    Deliberately carries no title: the tile already prints the game's name underneath,
    and a wordmark here just says it twice.
    """
    rgb = _dominant_color(png_bytes)
    if rgb is None:
        return None
    rgb = _normalize(rgb)

    # Keep the ground dark enough that the white tile text stays readable.
    top = _shade(rgb, 0.55)
    bottom = _shade(rgb, 0.20)
    glow = _shade(rgb, 1.15, floor=40)

    icon = base64.b64encode(png_bytes).decode("ascii")

    return (
        # An explicit width/height matters: with only a viewBox the image has no
        # intrinsic size, and the tile's "is this a tiny icon?" test reads a bogus
        # naturalWidth and shrinks the card into the middle of its own tile.
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="600" height="900" viewBox="0 0 600 900">'
        f'<defs>'
        f'<linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="rgb{top}"/>'
        f'<stop offset="100%" stop-color="rgb{bottom}"/>'
        f'</linearGradient>'
        f'<radialGradient id="glow" cx="50%" cy="42%" r="52%">'
        f'<stop offset="0%" stop-color="rgb{glow}" stop-opacity=".55"/>'
        f'<stop offset="100%" stop-color="rgb{glow}" stop-opacity="0"/>'
        f'</radialGradient>'
        f'</defs>'
        f'<rect width="600" height="900" fill="url(#g)"/>'
        f'<rect width="600" height="900" fill="url(#glow)"/>'
        # Sits a little above centre so it stays clear of the tile's name overlay.
        f'<image x="110" y="188" width="380" height="380" '
        f'xlink:href="data:image/png;base64,{icon}"/>'
        f'</svg>'
    ).encode("utf-8")


# -- public --------------------------------------------------------------


def _wide_card_svg(name, image_path):
    """Letterbox a landscape banner onto a 2:3 card.

    Some titles only ever have a wide header — brand new releases, mostly. Cropping one
    into the tile keeps about a third of it, so band it across a tinted ground instead.
    The tint comes from the name rather than the image, since the banner is a JPEG and
    decoding it here would need a second decoder for no real gain.
    """
    try:
        with open(image_path, "rb") as fh:
            payload = fh.read()
    except OSError:
        return None
    if len(payload) < 2048:
        return None

    digest = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    hue = int(digest[:4], 16) % 360
    banner = base64.b64encode(payload).decode("ascii")
    mime = "png" if payload[:8] == b"\x89PNG\r\n\x1a\n" else "jpeg"

    # 460x215 is Steam's header shape; 600 wide keeps that ratio at 280 tall.
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="600" height="900" viewBox="0 0 600 900">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="hsl({hue},34%,30%)"/>'
        f'<stop offset="100%" stop-color="hsl({(hue + 40) % 360},38%,14%)"/>'
        f"</linearGradient></defs>"
        f'<rect width="600" height="900" fill="url(#g)"/>'
        f'<image x="0" y="310" width="600" height="280" '
        f'xlink:href="data:image/{mime};base64,{banner}"/>'
        f"</svg>"
    ).encode("utf-8")


def _write_card(png_bytes, dest):
    """Render a cover card from icon bytes. False when they could not be decoded."""
    if not png_bytes:
        return False
    svg = _card_svg(png_bytes)
    if not svg:
        return False
    with open(dest, "wb") as fh:
        fh.write(svg)
    return True


def _paths(game_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", game_id)
    join = lambda ext: os.path.join(config.ART_DIR, safe + ext)  # noqa: E731
    return join(".jpg"), join(".png"), join(".card.svg"), join(".miss")


def cache_state(game):
    """(kind, version) for art already on disk, without resolving anything.

    Called for every game on every /api/games, so it must never touch the network.
    `kind` is None when nothing is cached yet and the client should wait and see what
    /api/art actually returns. `version` busts the browser cache when art changes.
    """
    jpg, png, card, miss = _paths(game["id"])
    newest = 0
    for path in (jpg, png, card, miss):
        try:
            newest = max(newest, int(os.path.getmtime(path)))
        except OSError:
            pass

    if game.get("art_override"):
        kind = "cover"
    elif os.path.exists(jpg):
        kind = "cover"
    elif os.path.exists(card):
        kind = "card"
    elif os.path.exists(png):
        # The .png is the raw icon a card gets built from, not something served on its
        # own. Claiming "icon" here would style the tile for a 32px icon and then get a
        # full-size card, so say nothing and let the image settle it once it loads.
        kind = None
    elif os.path.exists(miss) and time.time() - os.path.getmtime(miss) < 86400:
        kind = "placeholder"
    else:
        kind = None
    return kind, newest


def resolve(game, cfg):
    """Return (path, kind) for a game's art, or (None, 'placeholder')."""
    config.ensure_dirs()
    jpg, png, card, miss = _paths(game["id"])

    if os.path.exists(jpg):
        return jpg, "cover"
    if os.path.exists(card):
        return card, "card"

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

    # 2. Art the source itself handed us. Epic's catalog carries real portrait store
    # art, which is authoritative in a way a fuzzy Steam name match never is.
    supplied = game.get("art_url")
    if supplied and _download(supplied, jpg):
        return jpg, "cover"

    # 3/4. CDN, by known appid or by name match.
    if not appid:
        appid = guess_appid(game["name"])
        if appid:
            game["steam_appid"] = appid  # surfaced in the UI so a bad guess is fixable

    if appid and _download(_CDN.format(appid=appid), jpg):
        return jpg, "cover"

    # 4b. SteamGridDB, which covers what Steam has no appid for at all.
    if _steamgriddb_cover(game, cfg, jpg):
        return jpg, "cover"

    # 5. A landscape header is better letterboxed than lost.
    if appid:
        wide = _local_steam_wide(cfg, appid)
        if wide:
            svg = _wide_card_svg(game["name"], wide)
            if svg:
                with open(card, "wb") as fh:
                    fh.write(svg)
                return card, "card"

    # 6. Build a cover from artwork the game already ships.
    exe = game.get("exe_path")
    if exe and winpath.exists(exe) and _extract_icon(exe, png):
        try:
            with open(png, "rb") as fh:
                source = fh.read()
        except OSError:
            source = None
        if _write_card(source, card):
            return card, "card"
        if source:
            return png, "icon"  # the icon exists but would not decode; better than none

    # Store and Xbox packages have no exe to read, but do carry their own tile art.
    if _write_card(_package_logo(game.get("install_dir")), card):
        return card, "card"

    # 7. Nothing worked; remember that for a day so we do not retry on every load.
    open(miss, "w").close()
    return None, "placeholder"


def placeholder_svg(name):
    """Deterministic lettered card, used when every lookup fails."""
    digest = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    hue = int(digest[:4], 16) % 360
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name or "?")[:2]).upper()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="600" height="900" viewBox="0 0 600 900">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="hsl({hue},38%,32%)"/>'
        f'<stop offset="100%" stop-color="hsl({(hue + 40) % 360},42%,16%)"/>'
        f"</linearGradient></defs>"
        f'<rect width="600" height="900" fill="url(#g)"/>'
        f'<text x="300" y="470" font-family="Segoe UI,Arial,sans-serif" font-size="210" '
        f'font-weight="700" fill="rgba(255,255,255,.82)" text-anchor="middle">'
        f"{initials or '?'}</text></svg>"
    ).encode("utf-8")
