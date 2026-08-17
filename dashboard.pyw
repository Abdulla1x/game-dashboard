"""Entry point. Run with pythonw.exe so no console window appears.

Started automatically at login by the Startup shortcut that install.py creates. Safe to
run twice: the second instance sees the port is taken and exits without complaint.
"""

import socket
import sys

import config
import server


def port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main():
    cfg = config.load()
    port = cfg["port"]

    if not port_is_free(port):
        print(f"[dashboard] already running on port {port}")
        return 0

    open_browser = "--open" in sys.argv
    server.serve(port, open_browser=open_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
