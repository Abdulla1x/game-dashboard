"""Epic Games account tokens.

Epic publishes no offline record of what an account owns — the `.item` manifests under
ProgramData only describe what is installed. Getting the full library means talking to
Epic's own services with the launcher's OAuth client, which is what this does.

It is a one-time interactive step:

    py epicauth.py

which prints a login URL, waits for the `authorizationCode` shown after signing in, and
exchanges it for tokens kept in `data/epic_auth.json`. After that the refresh token is
renewed automatically for as long as it stays valid; when it lapses, run the command
again. Nothing here is required for the dashboard to work — without it the Epic library
simply stays install-only.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import config

# The public credentials the Epic Games Launcher itself uses. They are not secret — the
# launcher ships them — and they are the only client permitted to read a user's library.
_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
_CLIENT_SECRET = "daafbccc737745039dffe53d94fc76cf"

_OAUTH = ("https://account-public-service-prod.ol.epicgames.com"
          "/account/api/oauth/token")
_REDIRECT = ("https://www.epicgames.com/id/api/redirect"
             f"?clientId={_CLIENT_ID}&responseType=code")
_LOGIN = ("https://www.epicgames.com/id/login"
          f"?redirectUrl={urllib.parse.quote(_REDIRECT, safe='')}")

AUTH_JSON = os.path.join(config.DATA_DIR, "epic_auth.json")

_UA = {"User-Agent": "UELauncher/11.0.1-14907503+++Portal+Release-Live "
                     "Windows/10.0.19041.1.256.64bit"}


class EpicAuthError(Exception):
    pass


def _basic():
    raw = f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode("utf-8")
    return "basic " + base64.b64encode(raw).decode("ascii")


def _token_request(fields):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    headers = dict(_UA)
    headers["Authorization"] = _basic()
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(_OAUTH, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise EpicAuthError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EpicAuthError(str(exc)) from exc


def _store(payload):
    now = int(time.time())
    data = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        # Renew a little early rather than racing the expiry mid-scan.
        "expires_at": now + int(payload.get("expires_in", 0)) - 120,
        "refresh_expires_at": now + int(payload.get("refresh_expires", 0)) - 120,
        "account_id": payload.get("account_id"),
        "display_name": payload.get("displayName"),
    }
    config.write_json(AUTH_JSON, data)
    return data


def login(code):
    """Exchange an authorizationCode for tokens and store them."""
    code = (code or "").strip().strip('"')
    if not code:
        raise EpicAuthError("no authorization code given")
    data = _store(_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "token_type": "eg1",
    }))
    return data


def access_token():
    """A usable access token, refreshing if needed. None when not logged in."""
    data = config.read_json(AUTH_JSON, None)
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None

    now = int(time.time())
    if data.get("access_token") and now < data.get("expires_at", 0):
        return data["access_token"]

    if now >= data.get("refresh_expires_at", 0):
        print("[epic] saved login has expired; run `py epicauth.py` to sign in again")
        return None

    try:
        data = _store(_token_request({
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "token_type": "eg1",
        }))
    except EpicAuthError as exc:
        print(f"[epic] could not refresh login: {exc}")
        return None
    return data.get("access_token")


def main():
    config.ensure_dirs()
    print("Epic Games sign-in\n" + "=" * 60)
    print("1. Open this URL and sign in:\n")
    print(f"   {_LOGIN}\n")
    print("2. You will land on a page showing JSON that contains "
          '"authorizationCode".')
    print("3. Paste that code (just the code) below.\n")
    try:
        code = input("authorizationCode: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1

    try:
        data = login(code)
    except EpicAuthError as exc:
        print(f"\nSign-in failed: {exc}")
        return 1

    who = data.get("display_name") or data.get("account_id") or "your account"
    print(f"\nSigned in as {who}. Owned Epic games will appear after the next rescan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
