"""Minimal Valve KeyValues (VDF) parser.

Handles the subset needed for Steam's `libraryfolders.vdf` and `appmanifest_*.acf`:
quoted and bare tokens, nested braces, `//` line comments, and backslash escapes.
"""

import re

# Order matters: quoted strings come first so a `//` inside a quoted Windows path
# is consumed as string content rather than treated as a comment.
_TOKEN = re.compile(
    r'"((?:[^"\\]|\\.)*)"'   # 1: quoted string
    r"|//[^\n]*"             #    line comment (skipped)
    r"|(\{)"                 # 2: open brace
    r"|(\})"                 # 3: close brace
    r"|([^\s{}\"]+)"         # 4: bare token
)

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape(s):
    if "\\" not in s:
        return s
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads(text):
    """Parse VDF text into nested dicts. Duplicate keys: last one wins."""
    root = {}
    stack = [root]
    key = None

    for m in _TOKEN.finditer(text):
        quoted, open_b, close_b, bare = m.groups()

        if open_b:
            child = {}
            # A brace with no preceding key is malformed; parent it under "" rather
            # than crashing, so one bad file cannot take down a whole scan.
            stack[-1][key if key is not None else ""] = child
            stack.append(child)
            key = None
        elif close_b:
            if len(stack) > 1:
                stack.pop()
            key = None
        elif quoted is not None or bare is not None:
            token = _unescape(quoted) if quoted is not None else bare
            if key is None:
                key = token
            else:
                stack[-1][key] = token
                key = None
        # else: comment match, nothing to do

    return root


def load(path, encoding="utf-8"):
    with open(path, "r", encoding=encoding, errors="replace") as fh:
        return loads(fh.read())


def get(mapping, *names, default=None):
    """Case-insensitive lookup. Steam is inconsistent about key casing."""
    if not isinstance(mapping, dict):
        return default
    lowered = {k.lower(): v for k, v in mapping.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default
