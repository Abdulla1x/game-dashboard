"""Steam games you own but have not installed.

Two routes, best first:

1. `IPlayerService/GetOwnedGames`, when a Web API key and SteamID are configured. This
   is the only source that actually knows what the account owns, and it comes with real
   names and playtime.
2. Otherwise, the appids Steam has cached art for under `appcache/librarycache`. Steam
   fills that in for titles it shows in the library, so it is a decent proxy — but it
   also picks up DLC, adverts and delisted apps, so every candidate is checked against
   the store and only `type == "game"` survives. The verdicts are cached, because that
   is one request per appid the first time around.

Neither route is fatal if it fails: an empty list just means the "All" view shows only
what is installed.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config
import winpath

SOURCE = "steam"

_OWNED_URL = ("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
              "?key={key}&steamid={steamid}&include_appinfo=1"
              "&include_played_free_games=1&format=json")
_APPDETAILS = "https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic"

_UA = {"User-Agent": "Mozilla/5.0 (compatible; game-dashboard/1.0)"}

# Same family of non-games the installed scanner filters out.
_SKIP_NAME = re.compile(
    r"^(steamworks|steam linux runtime|proton|steam controller|steamvr|"
    r"spacewar|steam deck)", re.I
)

_TYPES_JSON = os.path.join(config.DATA_DIR, "steam_apptypes.json")


def _fetch_json(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[owned] request failed ({exc})")
        return None


def _record(appid, name, playtime_minutes=0, last_played=None):
    return {
        "id": f"steam:{appid}",
        "name": name,
        "source": SOURCE,
        "installed": False,
        "install_dir": None,
        # steam://install opens Steam's own install dialog, which asks for a library
        # folder rather than silently starting a download.
        "launch": {"kind": "url", "value": f"steam://install/{appid}"},
        "exe_name": None,
        "exe_path": None,
        "size_bytes": 0,
        "last_played": last_played or None,
        "steam_appid": int(appid),
        "playtime_minutes": playtime_minutes or 0,
    }


# -- route 1: the Web API ------------------------------------------------


def _from_api(cfg):
    key = (cfg.get("steam_api_key") or "").strip()
    steamid = str(cfg.get("steam_id") or "").strip()
    if not key or not steamid:
        return None

    data = _fetch_json(_OWNED_URL.format(key=key, steamid=steamid))
    games = ((data or {}).get("response") or {}).get("games")
    if not games:
        # A private "Game details" setting returns an empty response rather than an
        # error, so say so instead of silently showing nothing.
        print("[owned] Steam API returned no games — check the key, the SteamID, and "
              "that Steam privacy has 'Game details' set to Public")
        return None

    out = []
    for entry in games:
        name = (entry.get("name") or "").strip()
        appid = entry.get("appid")
        if not appid or not name or _SKIP_NAME.match(name):
            continue
        out.append(_record(appid, name,
                           entry.get("playtime_forever", 0),
                           entry.get("rtime_last_played")))
    print(f"[owned] Steam Web API: {len(out)} owned games")
    return out


# -- route 2: Steam's local art cache ------------------------------------


def _cached_appids(cfg):
    base = winpath.native(winpath.join(cfg["steam_root"], "appcache", "librarycache"))
    if not os.path.isdir(base):
        return []
    out = []
    for entry in os.listdir(base):
        if entry.isdigit() and os.path.isdir(os.path.join(base, entry)):
            out.append(entry)
    return out


def _from_cache(cfg, budget=150):
    appids = _cached_appids(cfg)
    if not appids:
        return []

    known = config.read_json(_TYPES_JSON, {})
    out, looked_up = [], 0

    for appid in appids:
        record = known.get(appid)
        if record is None:
            if looked_up >= budget:
                continue  # finish on the next scan rather than hammering the store
            data = _fetch_json(_APPDETAILS.format(appid=appid), timeout=15)
            looked_up += 1
            if data is None:
                # The store rate-limits, and a throttled request says nothing about the
                # app. Caching that as "not a game" would hide it permanently.
                continue
            entry = data.get(str(appid)) or {}
            body = entry.get("data") or {}
            record = ({"type": body.get("type"), "name": body.get("name")}
                      if entry.get("success") and body.get("name") else {})
            known[appid] = record
            time.sleep(0.3)  # stay under the store's rate limit
            if looked_up % 25 == 0:
                config.write_json(_TYPES_JSON, known)

        name = (record or {}).get("name")
        if not name or (record or {}).get("type") != "game":
            continue
        if _SKIP_NAME.match(name):
            continue
        out.append(_record(appid, name))

    if looked_up:
        config.write_json(_TYPES_JSON, known)
    pending = sum(1 for a in appids if a not in known)
    print(f"[owned] Steam local cache: {len(out)} games "
          f"({len(appids)} cached appids, {looked_up} looked up"
          + (f", {pending} still to classify — rescan to finish)" if pending else ")"))
    return out


def scan(cfg):
    if not cfg.get("include_owned", True):
        return []
    return _from_api(cfg) or _from_cache(cfg)
