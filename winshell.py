"""PowerShell bridge.

Used for the handful of things stdlib Python cannot do on Windows without pip:
resolving .lnk shortcut targets, enumerating Start Menu AUMIDs, and rendering icons
via System.Drawing. Works unchanged from WSL, where powershell.exe is on PATH.
"""

import json
import subprocess

import winpath

# cmd.exe/powershell.exe refuse a UNC working directory, which is what a WSL home
# directory looks like from Windows. Anchor to a real drive path when not on Windows.
_CWD = None if winpath.ON_WINDOWS else "/mnt/c"

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW, so pythonw never flashes a console


def run(script, timeout=60):
    """Run a PowerShell script, returning stdout as text ('' on failure)."""
    try:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=_CWD,
            creationflags=_NO_WINDOW if winpath.ON_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[winshell] {exc}")
        return ""

    if proc.returncode != 0 and proc.stderr.strip():
        print(f"[winshell] {proc.stderr.strip()[:200]}")
    return proc.stdout


def run_json(script, timeout=60):
    """Run a script that emits JSON; always returns a list."""
    # -Compress keeps output on one line; -Depth avoids silent truncation of nesting.
    out = run(f"{script} | ConvertTo-Json -Compress -Depth 4", timeout=timeout).strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if data is None:
        return []
    return data if isinstance(data, list) else [data]
