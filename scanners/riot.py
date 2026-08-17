"""Riot Games. Titles launch through RiotClientServices.exe, not their own binary."""

import os

import winpath

SOURCE = "riot"

# Folder name -> (display name, product id, patchline, process name for playtime)
_PRODUCTS = {
    "valorant": ("VALORANT", "valorant", "live", "VALORANT-Win64-Shipping.exe"),
    "league of legends": ("League of Legends", "league_of_legends", "live", "League of Legends.exe"),
    "legends of runeterra": ("Legends of Runeterra", "bacon", "live", "LoR.exe"),
}


def scan(cfg):
    root = cfg["riot_root"]
    native = winpath.native(root)
    if not os.path.isdir(native):
        return []

    client = winpath.join(root, "Riot Client", "RiotClientServices.exe")
    if not winpath.exists(client):
        print("[riot] RiotClientServices.exe not found; skipping")
        return []

    games = []
    for entry in sorted(os.listdir(native)):
        meta = _PRODUCTS.get(entry.lower())
        if not meta or not os.path.isdir(os.path.join(native, entry)):
            continue
        display, product, patchline, proc_name = meta

        games.append({
            "id": f"riot:{product}",
            "name": display,
            "source": SOURCE,
            "install_dir": winpath.join(root, entry),
            "launch": {
                "kind": "exe_args",
                "value": client,
                "args": [
                    f"--launch-product={product}",
                    f"--launch-patchline={patchline}",
                ],
            },
            "exe_name": proc_name,
            # Claim the shared client so Start Menu shortcuts pointing at it dedupe away.
            "exe_path": client,
            "size_bytes": 0,
            "last_played": None,
            "steam_appid": None,
        })
    return games
