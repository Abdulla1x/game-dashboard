# Contributing

Bug reports and PRs are welcome. This is a small project with a few firm rules.

## Running from source

```bat
git clone https://github.com/Abdulla1x/game-dashboard.git
cd game-dashboard
py server.py            :: runs with a visible console — use this while developing
```

`py server.py` is preferable to `pythonw dashboard.pyw` when working: `pythonw` has no
console, so every diagnostic goes nowhere and a failure looks like a dead port.

```bat
py -m unittest discover tests    :: must pass before a PR
py scan.py --dry-run -v          :: full table plus dedupe decisions, writes nothing
py scan.py --selftest            :: structural invariants over your real library
py detect.py                     :: what setup detection sees on this machine
```

Python changes need a server restart. `web/` is served from disk and only needs a reload.

## Rules

**Standard library only.** No pip, no npm, no build step. This runs from a double-clicked
shortcut on a machine with nothing installed but Python, and that is the feature. If
something seems to need a dependency, look for a stdlib or PowerShell route.

**Never widen the network surface.** The server binds `127.0.0.1`, has no authentication,
and starts executables. Do not change the bind address, add CORS headers, or add a tunnel.
New POST routes go through the same guard as the existing ones, and any value arriving over
HTTP that reaches the filesystem gets validated at the boundary.

**Do not rebuild the grid.** `web/app.js` reuses one DOM node per game id because
recreating `<img>` elements makes the whole library blink. If you add a rendered field, add
it to `signature()` too. See [ARCHITECTURE.md](ARCHITECTURE.md).

**Verify on Windows.** Development from WSL is supported and useful, but the two
environments genuinely disagree — Xbox app IDs only resolve in the Windows context, and
path separators have caused real bugs. A WSL-only pass is not a verification.

## Tests

`tests/` uses `unittest` from the standard library. Tests must build their own fixtures in
a temp directory and pass on any machine — never assert against whatever games happen to be
installed. That mistake is why an earlier version of the selftest silently checked nothing
for the life of the project.

If you change the executable heuristic, add a case to `tests/test_exefind.py` showing the
folder shape that was picking the wrong binary.

## Reporting a bug

Please include the output of:

```bat
py scan.py --dry-run -v
```

If it is about a specific game, the relevant few lines are enough — that output lists your
whole library, so trim it to what matters. For a wrong executable or missing cover, say
which game and what it picked instead.

For anything security-related, open an issue; there is no private channel, but please say
up front if you would rather discuss details privately first.
