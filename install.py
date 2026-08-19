"""One-time setup: build the icon and the two shortcuts. Idempotent — re-run any time.

Creates:
  Start Menu\\Programs\\Game Dashboard.lnk          -> chrome.exe --app=http://127.0.0.1:PORT
  Start Menu\\Programs\\Startup\\Game Dashboard Server.lnk -> pythonw.exe dashboard.pyw

Pin the *first* one to the taskbar. It targets the browser in app mode, which is what
gives the window its own AppUserModelID — so Windows treats it as a separate app with
its own taskbar button and its own icon, instead of grouping it under the browser.
"""

import os
import shutil
import struct
import sys

import config
import winpath
import winshell

APP_NAME = "Game Dashboard"
SERVER_SHORTCUT = "Game Dashboard Server"

BROWSERS = {
    "chrome": [
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ],
    "edge": [
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    ],
}


# -- icon ----------------------------------------------------------------

_ICON_PS = """
Add-Type -AssemblyName System.Drawing
$size = 256
$bmp = New-Object System.Drawing.Bitmap $size, $size
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$g.Clear([System.Drawing.Color]::Transparent)

# Rounded-square background with a blue -> violet gradient.
$rect = New-Object System.Drawing.Rectangle 0, 0, $size, $size
$brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
    $rect,
    [System.Drawing.ColorTranslator]::FromHtml('#6EA8FE'),
    [System.Drawing.ColorTranslator]::FromHtml('#A07CFF'),
    [System.Drawing.Drawing2D.LinearGradientMode]::ForwardDiagonal)
$path = New-Object System.Drawing.Drawing2D.GraphicsPath
$r = 56
$path.AddArc(0, 0, $r, $r, 180, 90)
$path.AddArc($size - $r, 0, $r, $r, 270, 90)
$path.AddArc($size - $r, $size - $r, $r, $r, 0, 90)
$path.AddArc(0, $size - $r, $r, $r, 90, 90)
$path.CloseFigure()
$g.FillPath($brush, $path)

# White controller body.
$white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
$body = New-Object System.Drawing.Drawing2D.GraphicsPath
$br = 72
$body.AddArc(40, 92, $br, $br, 90, 180)
$body.AddArc(144, 92, $br, $br, 270, 180)
$body.CloseFigure()
$g.FillPath($white, $body)

# Dark d-pad and buttons punched into it.
$dark = New-Object System.Drawing.SolidBrush (
    [System.Drawing.ColorTranslator]::FromHtml('#12151D'))
$g.FillRectangle($dark, 82, 108, 14, 44)
$g.FillRectangle($dark, 67, 123, 44, 14)
$g.FillEllipse($dark, 158, 111, 20, 20)
$g.FillEllipse($dark, 180, 135, 20, 20)

$bmp.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


def build_icon():
    """Render a PNG with System.Drawing, then wrap it in an ICO container."""
    png_path = os.path.join(config.DATA_DIR, "icon_source.png")
    ico_path = os.path.join(config.ROOT, "icon.ico")

    winshell.run(_ICON_PS.replace("{out}", winpath.windows(png_path)), timeout=60)
    if not os.path.exists(png_path) or os.path.getsize(png_path) < 200:
        print("  ! icon render failed; leaving any existing icon in place")
        return ico_path if os.path.exists(ico_path) else None

    with open(png_path, "rb") as fh:
        png = fh.read()

    # ICO with a single PNG-compressed 256x256 entry (supported since Vista).
    # A width/height byte of 0 means 256.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    with open(ico_path, "wb") as fh:
        fh.write(header + entry + png)

    os.makedirs(config.WEB_DIR, exist_ok=True)
    shutil.copyfile(ico_path, os.path.join(config.WEB_DIR, "favicon.ico"))
    os.remove(png_path)
    print(f"  icon.ico + web/favicon.ico ({len(png) // 1024} KB)")
    return ico_path


# -- shortcuts -----------------------------------------------------------


def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def create_shortcut(path, target, arguments="", workdir="", icon="", description=""):
    script = (
        "$s = New-Object -ComObject WScript.Shell; "
        f"$k = $s.CreateShortcut({_ps_quote(path)}); "
        f"$k.TargetPath = {_ps_quote(target)}; "
        f"$k.Arguments = {_ps_quote(arguments)}; "
        f"$k.WorkingDirectory = {_ps_quote(workdir)}; "
        f"$k.Description = {_ps_quote(description)}; "
    )
    if icon:
        script += f"$k.IconLocation = {_ps_quote(icon)}; "
    script += "$k.Save()"
    winshell.run(script, timeout=45)
    return os.path.exists(winpath.native(path))


def find_browser(cfg):
    """(exe, mode) for the configured browser; mode 'app' supports --app windows."""
    choice = (cfg.get("browser") or "chrome").lower()
    if choice == "default":
        return None, "default"

    for name in ([choice] if choice in BROWSERS else []) + ["chrome", "edge"]:
        for candidate in BROWSERS.get(name, []):
            if winpath.exists(candidate):
                if name != choice:
                    print(f"  ! {choice} not found; using {name}")
                return candidate, "app"

    print("  ! no Chrome or Edge found; falling back to the default browser")
    return None, "default"


def pythonw_path():
    """pythonw.exe next to the running interpreter, else whatever `py` resolves to."""
    if winpath.ON_WINDOWS:
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    out = winshell.run(
        "$p = (& py -c \"import sys,os;"
        "print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))\"); $p"
    ).strip()
    first = [line.strip() for line in out.splitlines() if line.strip().endswith(".exe")]
    return first[0] if first else "pythonw.exe"


def start_menu_dir(cfg):
    if winpath.ON_WINDOWS:
        base = os.path.expandvars("%APPDATA%")
    else:
        base = f"C:\\Users\\{config.windows_user(cfg)}\\AppData\\Roaming"
    return winpath.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def main():
    config.ensure_dirs()
    cfg = config.load()
    port = cfg["port"]
    url = f"http://127.0.0.1:{port}"
    root_win = winpath.windows(config.ROOT)

    print(f"Installing {APP_NAME}\n  project: {root_win}\n  url:     {url}\n")

    icon = build_icon()
    icon_win = winpath.windows(icon) if icon else ""

    programs = start_menu_dir(cfg)
    startup = winpath.join(programs, "Startup")
    if not winpath.isdir(programs):
        print(f"  ! Start Menu not found at {programs}")
        return 1

    # 1. The server, started headless at login.
    pyw = pythonw_path()
    server_lnk = winpath.join(startup, f"{SERVER_SHORTCUT}.lnk")
    ok = create_shortcut(
        server_lnk,
        target=pyw,
        arguments=f'"{root_win}\\dashboard.pyw"',
        workdir=root_win,
        icon=icon_win,
        description=f"{APP_NAME} background server",
    )
    print(f"  {'ok ' if ok else '!! '}Startup\\{SERVER_SHORTCUT}.lnk -> {pyw}")

    # 2. The window. This is the shortcut to pin.
    browser, mode = find_browser(cfg)
    app_lnk = winpath.join(programs, f"{APP_NAME}.lnk")
    if mode == "app":
        args = f'--app={url} --window-size={cfg.get("window_size", "1400,900")}'
        ok = create_shortcut(app_lnk, target=browser, arguments=args,
                             workdir=root_win, icon=icon_win, description=APP_NAME)
        print(f"  {'ok ' if ok else '!! '}{APP_NAME}.lnk -> {winpath.basename(browser)} --app")
    else:
        ok = create_shortcut(app_lnk, target=url, workdir=root_win,
                             icon=icon_win, description=APP_NAME)
        print(f"  {'ok ' if ok else '!! '}{APP_NAME}.lnk -> {url} (default browser)")

    print(f"""
Done.

  1. Start the server now:   pythonw dashboard.pyw     (or just reboot)
  2. Open the Start Menu, find "{APP_NAME}"
  3. Right-click it -> Pin to taskbar

Pin "{APP_NAME}", not "{SERVER_SHORTCUT}" — the first opens the window, the
second is the background service that already runs at login.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
