"""Epic games you own but have not installed.

Reads the account's library through Epic's own services and resolves each entry against
the catalog, which is what supplies real titles and tells apart a game from the mass of
DLC, plugins and Unreal Engine assets that also live in a library.

Requires a one-time `py epicauth.py`; without it this yields nothing and the Epic side
of the dashboard stays install-only.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import epicauth

SOURCE = "epic"

# The catalog is queried per namespace, and a library spreads across dozens of them —
# each one a fresh connection and TLS handshake. Titles do not change, so the answers
# are cached the same way owned_steam caches its store verdicts, and a rescan costs
# nothing. BUDGET caps the first run: whatever has not resolved yet is left for next
# time, because the whole owned library is additive.
CATALOG_JSON = os.path.join(config.DATA_DIR, "epic_catalog.json")
BUDGET = 45.0

_LIBRARY = ("https://library-service.live.use1a.on.epicgames.com"
            "/library/api/public/items?includeMetadata=true")
_CATALOG = ("https://catalog-public-service-prod06.ol.epicgames.com"
            "/catalog/api/shared/namespace/{ns}/bulk/items"
            "?country=US&locale=en&includeDLCDetails=false&includeMainGameDetails=false")

# Library entries that are not games. Epic files engine content, plugins and add-ons in
# the same list as the games themselves.
_SKIP_CATEGORIES = {"addons", "digitalextras", "plugins", "projects", "engines",
                    "assets", "software"}


def _get(url, token, timeout=12):
    headers = {"Authorization": f"bearer {token}",
               "User-Agent": "UELauncher/11.0.1 Windows/10.0.19041.1.256.64bit"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[epic] request failed ({exc})")
        return None


def _library_records(token):
    """Every library entry, following Epic's cursor pagination."""
    out, url, guard = [], _LIBRARY, 0
    while url and guard < 40:
        data = _get(url, token)
        if not data:
            break
        out.extend(data.get("records") or [])
        cursor = (data.get("responseMetadata") or {}).get("nextCursor")
        url = f"{_LIBRARY}&cursor={urllib.parse.quote(cursor)}" if cursor else None
        guard += 1
    return out


def _catalog(token, namespace, item_ids, cache, deadline):
    """Catalog metadata for one namespace, in batches the service will accept.

    Anything already cached is answered locally; only the remainder goes to the network,
    and only while there is budget left.
    """
    found = {item: cache[item] for item in item_ids if item in cache}
    missing = [item for item in item_ids if item not in cache]

    for start in range(0, len(missing), 40):
        if time.monotonic() > deadline:
            break
        batch = missing[start:start + 40]
        url = _CATALOG.format(ns=urllib.parse.quote(namespace))
        url += "".join(f"&id={urllib.parse.quote(i)}" for i in batch)
        data = _get(url, token)
        if isinstance(data, dict):
            found.update(data)
            cache.update(data)
    return found


# Epic ships its own portrait store art. Preferred over guessing a Steam appid from the
# title, which is the only other option for a game Steam does not sell.
_TALL_IMAGES = ("DieselGameBoxTall", "DieselStoreFrontTall", "OfferImageTall",
                "DieselGameBoxLogo", "Thumbnail")


def _tall_image(item):
    by_type = {}
    for image in item.get("keyImages") or []:
        url, kind = image.get("url"), image.get("type")
        if url and kind and kind not in by_type:
            by_type[kind] = url
    for kind in _TALL_IMAGES:
        if by_type.get(kind):
            return by_type[kind]
    return None


def _is_game(item):
    paths = {(c.get("path") or "").lower() for c in (item.get("categories") or [])}
    if paths & _SKIP_CATEGORIES:
        return False
    if item.get("mainGameItem"):
        return False  # an add-on pointing at its base game
    return "games" in paths or "applications" in paths


def scan(cfg):
    if not cfg.get("include_owned", True):
        return []

    token = epicauth.access_token()
    if not token:
        return []

    records = _library_records(token)
    if not records:
        return []

    # Group by namespace: the catalog is queried per namespace, not globally.
    by_namespace = {}
    for rec in records:
        ns, item = rec.get("namespace"), rec.get("catalogItemId")
        app = rec.get("appName")
        if ns and item and app:
            by_namespace.setdefault(ns, {})[item] = app

    cache = config.read_json(CATALOG_JSON, {})
    if not isinstance(cache, dict):
        cache = {}
    cached_at_start = len(cache)
    deadline = time.monotonic() + BUDGET

    games = []
    for namespace, items in by_namespace.items():
        catalog = _catalog(token, namespace, list(items), cache, deadline)
        for item_id, app_name in items.items():
            item = catalog.get(item_id)
            if not isinstance(item, dict) or not _is_game(item):
                continue
            title = (item.get("title") or "").strip()
            if not title:
                continue
            games.append({
                "id": f"epic:{app_name}",
                "name": title,
                "source": SOURCE,
                "installed": False,
                "install_dir": None,
                "launch": {
                    "kind": "url",
                    "value": (f"com.epicgames.launcher://apps/{app_name}"
                              "?action=install"),
                },
                "exe_name": None,
                "exe_path": None,
                "size_bytes": 0,
                "last_played": None,
                "steam_appid": None,
                "art_url": _tall_image(item),
            })

    print(f"[owned] Epic library: {len(games)} games ({len(records)} entries)")
    if len(cache) != cached_at_start:
        config.write_json(CATALOG_JSON, cache)
    if time.monotonic() > deadline:
        print("[epic] catalog budget reached; the rest resolves on the next scan")

    return games
