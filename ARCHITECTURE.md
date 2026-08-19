# Architecture

Notes on how this fits together, and on the decisions that were expensive to get right.
If you are changing something here, the reasoning matters more than the code.

## Constraints

**Standard library only.** No pip, no npm, no build step. The whole point is that it runs
from a double-clicked shortcut on a machine with nothing installed but Python. If
something seems to need a dependency, there is almost always a stdlib or PowerShell route.

**Loopback only, no auth.** The server starts arbitrary executables. It must never be
reachable from the network, and the bind address is not a configurable setting.

**`data/` is generated and gitignored.** It holds a full inventory of the user's installed
games with file paths, downloaded cover art, and — if Epic sign-in is used — a live OAuth
refresh token. All of it rebuilds from a scan.

## The pipeline

```
detect.py ──▶ config.json ──▶ scan.py ──▶ data/library.json ──▶ server.py ──▶ web/
              (where games      (scanners,                       (HTTP,
               live)             merge, dedupe,                    art on demand)
                                 overrides)
```

`scan.py` runs each scanner in priority order, merges the results, dedupes twice, applies
`data/overrides.json`, and writes `library.json`. The server reads that file, serves it as
JSON, and resolves cover art lazily per tile.

Scanners are deliberately independent and best-effort: each is wrapped so a failure prints
and yields `[]` rather than taking the scan with it. One broken launcher must not cost you
the rest of your library.

## Windows paths, tested from WSL

Production is Windows Python. Development often happens from WSL. `winpath.py` bridges
them: scanners speak **Windows** paths canonically — that is what gets stored and handed
to launchers — and call `winpath.native()` only when they actually touch the disk.

The two environments do not agree, so **verify on Windows before calling anything done.**
`Get-StartApps` only resolves Xbox app IDs in the Windows context, so a WSL run reports one
fewer game and a WSL-only pass will silently miss that.

This split has bitten before. `exefind._walk` measured directory depth by counting `/`,
which is correct for the WSL form and useless for the Windows one — every path measured as
depth 0, the depth limit never fired, and each game folder was walked to the bottom.
Anything that inspects a path's *shape* has to handle both separators.

## The executable heuristic (`exefind.py`)

A folder is a game only if a plausible executable survives. Reject lists by parent
directory and by filename, then scoring on name similarity, depth, and size. A deeper
retry at depth 6 runs only when the normal depth-4 pass finds nothing, because scanning
that deep everywhere is slow and noisy.

The gate is load-bearing. Clip recorders create a folder per game containing only video,
sitting right beside real installs; loosen the gate and each becomes a phantom game. Tune
the reject lists when a new game picks the wrong binary — and add a case to
`tests/test_exefind.py`, which builds its own trees rather than asserting against whatever
happens to be installed.

`quick_probe()` is a separate, bounded version used only by setup detection to *count*
likely games. `pick()` stays the authority on what a game actually is. Running `pick()`
across every folder on every drive takes minutes, because each miss is re-walked at depth 6.

## Dedupe

The same game reaches the merge from two sources constantly. It happens twice:

1. **By path containment, in both directions.** A launcher may install to a subdirectory of
   a folder the folder scanner also sees, so the parent case matters as much as the child.
2. **By normalised name**, with a source ranking that prefers launcher-backed records over
   bare folders — they carry better metadata and start the game the way it expects.

Owned-but-not-installed records are collected *after* everything on disk has been claimed,
so they can only ever add. An owned record must have no `install_dir` and must carry an
install URL; installed always outranks owned.

## Cover art (`art.py`)

The fallback chain is in the README. Two things are easy to break:

- **Steam has two cache layouts** — files directly under the appid directory, and newer
  builds nesting each file in a hash-named subdirectory. Missing the second sends installed
  games to the network and then to a blurry icon.
- **Name matching needs a length guard**, or "God of War" matches "God of War Ragnarok".

`peicon.py` reads the largest icon out of a PE binary with `struct` and `zlib`, because
PowerShell's `ExtractAssociatedIcon` only ever returns 32×32. It handles both icon
flavours: modern PNG entries pass through, older bottom-up DIBs are re-encoded — note the
stored height is doubled to cover the AND mask.

A game with no cover gets a generated full-bleed 2:3 card tinted from its icon's dominant
colour, kept off both ends of the luminance range so near-black and near-white icons do
not produce flat grey cards.

## The grid must not be rebuilt

`web/app.js` reuses one DOM node per game id. This is not a micro-optimisation: wiping the
grid recreates every `<img>`, and a fresh `<img>` paints its empty background before the
bytes arrive, so the whole library blinks. That was a visible flicker on every poll and on
every search keystroke.

If you touch rendering, keep all three:

- `refresh()` compares a `signature()` of the payload and skips `render()` when nothing
  visible changed. **Any new rendered field must go into `signature()`.**
- `render()` reuses nodes and moves only the ones in the wrong place.
- `updateCard()` reassigns `img.src` only when the URL actually changed. That URL carries
  an art version, so corrected art still busts the browser cache — art is served with a
  long `max-age`, which is why a stale cover could otherwise persist for a day.

`server.py` speaks HTTP/1.1 with ETag/304 for the same reason: under HTTP/1.0 every tile
opened its own connection.

## The request guard

Binding `127.0.0.1` keeps the network out. It does **not** keep out the user's own
browser — any page they have open can send requests to loopback, and the same-origin policy
stops it reading the response, not sending the request.

So every request passes `_guard()`:

- **`Host` must match** the bound address, which is what defeats DNS rebinding.
- **`Origin`, when present, must be same-origin.**
- **POST must be `Content-Type: application/json`.** This is the load-bearing one: it is
  what forces a CORS preflight, and since no CORS headers are ever emitted, the preflight
  fails and the request never arrives. Without it, a POST is a "simple request" that gets
  sent regardless.

A CSRF token was considered and rejected. It would require templating `index.html` at serve
time, which breaks its ETag/304 path and adds a session concept to a stdlib server, and it
defends only against a bug in one of the three checks above. It offers nothing against
local malware, which can read the token off disk anyway.

**When adding a route:** anything that reaches the filesystem gets a validator, and values
that arrive over HTTP are validated at the boundary — not deeper in. A hand-edited
`overrides.json` may legitimately point anywhere; an HTTP request may not. Keeping that
distinction at the edge is what lets the documented hand-edit workflow stay permissive
while the network surface stays narrow.

### Manual entries move the trust boundary

Before **+ Add**, no HTTP request could cause a binary the scanners had not already found
to run. That is no longer true, and pretending otherwise would be worse than saying it.

`_v_target` is defence in depth around a feature whose entire purpose is to run a program
the dashboard has never seen; `_guard()` is what stops another origin reaching it at all.
The target must already exist on disk, carry a `.exe`/`.lnk`/`.url` extension, and sit
outside `%SystemRoot%` — one rule that rules out `cmd.exe`, `powershell.exe`, `mshta.exe`
and every other System32 binary worth borrowing, with no list to keep up to date. A URL
must match a scheme allowlist, which refuses `file:`, `javascript:`, `data:` and
`ms-msdt:` by construction.

Note the ordering inside the validator: a Windows drive letter is itself a syntactically
valid URL scheme, so `C:\Games\X.exe` parses as the scheme `c:`. The path branch has to be
tested first or every real path is classified as a rejected URL.

`shell:AppsFolder\<AUMID>` is the hand-edit/HTTP split in one line: `_blank_record`
supports the `shell` launch kind, and `_v_target` refuses the scheme. Launching any
installed Store app is real surface for no motivating case.

### `/api/apps` is a separate route on purpose

`_override` refuses any id not already in the library, which is what keeps the reserved
list keys `extra_games` and `owned_games` unreachable — a dict written to one of those
breaks every later scan. `/api/apps` needs to write `extra_games`, so it cannot ride on
that route. Its rule is the mirror image: an update may only touch an id that is *already*
an entry in `extra_games`. That keeps `owned_games` and every scanner-found game out of
reach, and it gives the product rule — you may only remove what you added. Everything else
is Hide, because a rescan would bring it back anyway.

### Companion apps

One game, a list of other library ids, launched alongside it. Three things hold it up:

- **The game goes first.** It was what got clicked; a broken helper must never cost you it.
  A companion that fails is collected and reported, never raised.
- **One level, always.** `launch_companions` calls the launcher directly and never
  re-enters itself, so a cycle is impossible to build rather than merely unlikely. No
  visited-set is needed, and none should be added.
- **Companions are not tracked.** Playtime watches are keyed by executable name and later
  calls overwrite earlier ones, so watching a helper would both invent sessions for it and
  let it steal the key from a game sharing its binary name.

A companion id that no longer resolves is reported, not pruned: folder ids are
path-derived, so an offline drive makes a game disappear, and silently deleting the user's
configuration for that is worse than a skipped launch. `_v_companions` keeps an id that is
already stored for the same reason.

### `apply_overrides` only ever sets

It is written for freshly built records, so it sets derived fields — `hidden`,
`art_override`, `companions` — and never clears them. That is fine during a scan and wrong
on the in-place path `/api/override` uses to update the UI without rescanning: a cleared
rule left the last value stuck on the live record until the next scan, which is why Unhide
used to appear to do nothing. The route pops those keys before re-applying. Any new
rule-derived field has to be popped there too.

## Where the fiddly bits are

- `scanners/shortcuts.py` runs last, fills gaps, and is deliberately conservative — the
  Start Menu is mostly not games, and its deny list is what keeps Office and editors out.
- `scanners/owned_steam.py`'s no-key fallback is noisy (DLC, adverts, delisted apps), so
  every appid is checked against the store and cached. Do not remove that filter.
- `scanners/owned_epic.py` queries the catalog per namespace, and a library spans dozens
  of them. Results are cached, because otherwise every scan is dozens of TLS handshakes.
- `api.steampowered.com` may be unreachable where `steamcommunity.com` is not; the
  community search route needs no key.
- SteamGridDB disables itself for the rest of the run after its first failure, by design.
  Saving a new key clears that latch — otherwise a corrected key appears to do nothing.
