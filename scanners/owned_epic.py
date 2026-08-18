"""Epic games you own but have not installed.

Reads the account's library through Epic's own services and resolves each entry against
the catalog, which is what supplies real titles and tells apart a game from the mass of
DLC, plugins and Unreal Engine assets that also live in a library.

Requires a one-time `py epicauth.py`; without it this yields nothing and the Epic side
of the dashboard stays install-only.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import epicauth

SOURCE = "epic"

_LIBRARY = ("https://library-service.live.use1a.on.epicgames.com"
            "/library/api/public/items?includeMetadata=true")
_CATALOG = ("https://catalog-public-service-prod06.ol.epicgames.com"
            "/catalog/api/shared/namespace/{ns}/bulk/items"
            "?country=US&locale=en&includeDLCDetails=false&includeMainGameDetails=false")

# Library entries that are not games. Epic files engine content, plugins and add-ons in
# the same list as the games themselves.
_SKIP_CATEGORIES = {"addons", "digitalextras", "plugins", "projects", "engines",
                    "assets", "software"}


def _get(url, token, timeout=30):
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


def _catalog(token, namespace, item_ids):
    """Catalog metadata for one namespace, in batches the service will accept."""
    found = {}
    for start in range(0, len(item_ids), 40):
        batch = item_ids[start:start + 40]
        url = _CATALOG.format(ns=urllib.parse.quote(namespace))
        url += "".join(f"&id={urllib.parse.quote(i)}" for i in batch)
        data = _get(url, token)
        if isinstance(data, dict):
            found.update(data)
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

    games = []
    for namespace, items in by_namespace.items():
        catalog = _catalog(token, namespace, list(items))
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
    return games
