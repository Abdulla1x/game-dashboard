"""Windows/WSL path translation.

The dashboard runs on Windows, but development and testing happen from WSL. Scanners
therefore speak *Windows* paths canonically (that is what gets stored in library.json
and handed to launchers), and call `native()` whenever they actually touch the disk.

On Windows both functions are near no-ops.
"""

import os
import re

ON_WINDOWS = os.name == "nt"

_DRIVE_WIN = re.compile(r"^([A-Za-z]):[\\/](.*)$", re.S)
_DRIVE_WSL = re.compile(r"^/mnt/([a-z])(?:/(.*))?$", re.S)


def native(path):
    """Windows path -> a path this interpreter can actually open."""
    if path is None:
        return None
    if ON_WINDOWS:
        return path
    m = _DRIVE_WIN.match(path)
    if m:
        drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return path


def windows(path):
    """Any path -> Windows form, for storage, display, and launching."""
    if path is None:
        return None
    if ON_WINDOWS:
        return path
    m = _DRIVE_WSL.match(path)
    if m:
        drive, rest = m.group(1).upper(), (m.group(2) or "")
        return f"{drive}:\\" + rest.replace("/", "\\")
    return path


def join(*parts):
    """Join Windows path components with backslashes, regardless of host OS."""
    cleaned = [p.replace("/", "\\").rstrip("\\") for p in parts if p]
    if not cleaned:
        return ""
    head, tail = cleaned[0], cleaned[1:]
    return "\\".join([head] + [p.lstrip("\\") for p in tail])


def drive_of(path):
    """Drive letter of a Windows path, or None."""
    m = _DRIVE_WIN.match(path or "")
    return m.group(1).upper() if m else None


def basename(path):
    return (path or "").replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]


def dirname(path):
    p = (path or "").replace("/", "\\").rstrip("\\")
    return p.rsplit("\\", 1)[0] if "\\" in p else p


def exists(win_path):
    return os.path.exists(native(win_path))


def isdir(win_path):
    return os.path.isdir(native(win_path))


def listdir(win_path):
    try:
        return sorted(os.listdir(native(win_path)))
    except (OSError, PermissionError):
        return []


def size(win_path):
    try:
        return os.path.getsize(native(win_path))
    except (OSError, PermissionError):
        return 0
