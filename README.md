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
4. The executable's embedded icon (shown centred, not stretched)
5. A generated lettered placeholder

A bad name match is fixable: right-click → **Fix cover art** → set the Steam app ID.

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
scanners/       steam, epic, xbox, riot, folders, shortcuts
exefind.py      the executable heuristic
art.py          cover art resolution
playtime.py     session tracking
vdf.py          Valve KeyValues parser
winpath.py      Windows/WSL path translation (lets the scanners be tested from WSL)
winshell.py     PowerShell bridge (.lnk targets, AUMIDs, icon rendering)
web/            index.html, app.js, style.css, favicon.ico
data/           library.json, overrides.json, playtime.json, art/  (generated)
```

## Security

The server binds `127.0.0.1` only and has no authentication, because it can start arbitrary
executables. Do not change the bind address or put it behind a tunnel.
