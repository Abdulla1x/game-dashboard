# Game Dashboard

One place to see and launch every game on this PC, across Steam, Epic, Xbox, Riot,
and the ~40 standalone folders scattered over four drives.

Pure Python 3 standard library — no pip installs, no build step.

## Install

```bat
py install.py
```

That renders `icon.ico` and creates two shortcuts:

| Shortcut | Where | What it does |
| --- | --- | --- |
| **Game Dashboard** | Start Menu | Opens the window. **Pin this one to the taskbar.** |
| Game Dashboard Server | Start Menu ▸ Startup | Runs the server headless at login |

Then: Start Menu → right-click **Game Dashboard** → *Pin to taskbar*.

Start the server now without rebooting:

```bat
start "" pythonw dashboard.pyw
```

### Why it opens in Chrome

The shortcut targets `chrome.exe --app=http://127.0.0.1:8777`. App mode gives the window
its own AppUserModelID, so Windows treats the dashboard as a separate app with its own
taskbar button and icon instead of grouping it under your browser. Firefox — the default
browser here — removed app-window mode, which is why it is not used even though it is the
system default.

Set `"browser": "edge"` in `config.json` to use Edge instead, or `"default"` to open a
plain tab in Firefox (simpler, but you lose the dedicated taskbar button).

## Use

- **Click a cover** to launch.
- **Installed / All** switches between what is on this PC and your whole library.
  Games you own but have not installed appear dimmed under **All**; clicking one asks
  first, then hands the download to its launcher.
- **`/`** focuses search. Type to filter instantly.
- **Source chips** filter by Steam / Epic / folder / etc.
- **Sort** by name, last played, playtime, or size on disk.
- **Right-click a cover** (or the `⋯` button) for: Open folder, Rename, Pick executable,
  Fix cover art, Hide.
- **Rescan** picks up newly installed games. **Measure sizes** walks the folder games to
  fill in size on disk — slow, so it runs in the background and is not automatic.

## How it finds things

| Source | Method |
| --- | --- |
| Steam | `libraryfolders.vdf` → each library's `appmanifest_*.acf` |
| Epic | `%ProgramData%\Epic\EpicGamesLauncher\Data\Manifests\*.item` |
| Xbox | `C:\XboxGames` matched to AUMIDs from `Get-StartApps` |
| Riot | `RiotClientServices.exe --launch-product=…` |
| Folders | `config.json` → `scan_roots`, using the executable heuristic below |
| Shortcuts | Start Menu `.lnk`/`.url`, conservatively filtered, runs last |
| Steam (owned) | `GetOwnedGames` with an API key, else the appids Steam has cached art for |
| Epic (owned) | The account's library service, after `py epicauth.py` |

### Games you do not have installed

Off by default only in the sense that they are hidden until you switch to **All**. Set
`"include_owned": false` in `config.json` to stop collecting them altogether.

**Steam.** Put a free key from [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey)
and your 64-bit SteamID in `config.json`:

```json
{ "steam_api_key": "…", "steam_id": "76561198…" }
```

That is the only source that truly knows what the account owns. It also needs Steam
privacy set to **Game details: Public**. Without a key the library is inferred from the
appids Steam has cached artwork for, which works but drags in DLC and delisted apps —
each candidate is checked against the store and only real games are kept, so the first
scan after enabling it is slow and the verdicts are cached in `data/steam_apptypes.json`.

**Epic.** Epic keeps no offline record of what you own, so it needs a one-time sign-in:

```bat
py epicauth.py
```

It prints a login URL and asks for the `authorizationCode` shown afterwards. Tokens live
in `data/epic_auth.json` and refresh themselves; when the refresh token finally lapses,
run it again.

**Everything else.** Xbox/Game Pass, Battle.net, Riot, Rockstar and EA expose no
ownership API that can be read without per-launcher OAuth, so they are listed by hand in
`data/overrides.json`:

```json
{
  "owned_games": [
    { "id": "xbox:forza-horizon-5",
      "name": "Forza Horizon 5",
      "source": "xbox",
      "install_url": "ms-windows-store://pdp/?productid=9NKX70BBCDRN" }
  ]
}
```

`install_url` is whatever protocol link the launcher uses — `steam://install/<appid>`,
`com.epicgames.launcher://apps/<id>?action=install`, `battlenet://<code>`,
`ms-windows-store://pdp/?productid=<id>`.
### The executable heuristic

A folder counts as a game **only if a plausible executable is found in it**. Redistributables,
uninstallers, crash handlers and console tools are rejected by name and by parent directory;
survivors are scored on name similarity to the folder, then depth, then size. Folders with no
surviving executable are skipped — which is what keeps `G:\VALORANT`, `G:\Counter-Strike 2`,
`G:\KovaaK's` and `G:\rocketleague` (Medal clip folders, no game) out of the library.

If nothing is found at depth 4, one deeper pass at depth 6 catches Unreal-style repacks that
bury the binary at `…\Gameface\Binaries\Win64\`.

### Cover art

1. Steam's own cache — `appcache\librarycache\<appid>\`, including the newer
   hash-subdirectory layout
2. Steam CDN by appid
3. For non-Steam games: name → appid via Steam's community search, then the CDN
4. SteamGridDB, if `steamgriddb_key` is set — this is what finds covers for the things
   Steam has no app ID for at all: Epic and Riot titles, launchers, mod clients
5. A cover generated here from the game's own artwork — the 256px icon in the
   executable, or the tile art an Xbox/Store package ships — centred on a gradient
   sampled from that icon, so it fills the tile like a real cover
6. A generated lettered placeholder

A bad name match is fixable: right-click → **Fix cover art** → set the Steam app ID.

Icons come out of the executable's PE resource directory (`peicon.py`). PowerShell's
`ExtractAssociatedIcon` is capped at 32×32, which is far too small to build a cover
from; it is only the fallback for binaries carrying no icon resource.

## Fixing what it gets wrong

Everything the UI writes lands in `data/overrides.json`, keyed by game id, and survives
rescans. You can also edit it directly:

```json
{
  "folder:EGames--Red Dead Redemption 2": { "exe": "Red Dead Redemption 2\\RDR2.exe" },
  "folder:EGames--UE_4.26": { "hidden": true },
  "folder:EGames--Stray": { "steam_appid": 1332010 },

  "extra_games": [
    { "id": "manual:gta3",
      "name": "GTA III Definitive Edition",
      "exe": "E:\\Games\\GTA The Trilogy Definitive Edition\\GTA.The.Trilogy.Definitive.Edition.v1.0.0.14377\\GTA III - Definitive Edition\\Gameface\\Binaries\\Win64\\LibertyCity.exe" }
  ]
}
```

`extra_games` is how you add a title the scanners cannot see — useful for the GTA Trilogy,
which is three games sharing one folder, so only one of them is detected automatically.

`exe` may be relative to the game's install folder or an absolute path.

## Playtime

Steam reports its own last-played time. For everything else the dashboard tracks its own:
after you launch a game it polls `tasklist` for the executable, opens a session on first
sighting and closes it when the process disappears. Sessions shorter than 30 seconds are
discarded. Data lives in `data/playtime.json`.

Because tracking is keyed on the executable name, a game whose real binary differs from the
detected one will not be tracked — fix it with **Pick executable**.

## Checking it still works

```bat
py scan.py --selftest     # asserts the decoy folders stay out, no duplicates, etc.
py scan.py --dry-run -v   # full table plus dedupe decisions, writes nothing
```

## Layout

```
dashboard.pyw   entry point (pythonw: no console window)
install.py      icon + shortcuts
server.py       HTTP server, 127.0.0.1 only
scan.py         runs scanners, merges, dedupes, applies overrides
scanners/       steam, epic, xbox, riot, folders, shortcuts,
                owned_steam, owned_epic
exefind.py      the executable heuristic
art.py          cover art resolution
peicon.py       largest icon out of a PE binary, and PNG encode/decode
epicauth.py     one-time Epic sign-in (py epicauth.py)
playtime.py     session tracking
vdf.py          Valve KeyValues parser
winpath.py      Windows/WSL path translation (lets the scanners be tested from WSL)
winshell.py     PowerShell bridge (.lnk targets, AUMIDs, icon rendering)
web/            index.html, app.js, style.css, favicon.ico
data/           library.json, overrides.json, playtime.json, art/,
                epic_auth.json, steam_apptypes.json  (generated)
```

## Security

The server binds `127.0.0.1` only and has no authentication, because it can start arbitrary
executables. Do not change the bind address or put it behind a tunnel.
