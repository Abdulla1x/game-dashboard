"""Validation on the two HTTP surfaces that reach the filesystem.

The server binds loopback and has no auth, which is safe only as long as a page in the
user's browser cannot talk it into doing something. These are the rules that hold that
line, so they are asserted rather than trusted.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class PathValidatorTest(unittest.TestCase):
    def test_accepts_a_windows_path(self):
        self.assertEqual(server._v_path("D:\\Games", "scan_roots"), "D:\\Games")

    def test_normalises_slashes_and_trailing_separator(self):
        self.assertEqual(server._v_path("D:/Games/", "scan_roots"), "D:\\Games")

    def test_keeps_a_bare_drive_root(self):
        self.assertEqual(server._v_path("G:\\", "scan_roots"), "G:\\")

    def test_rejects_a_posix_path(self):
        with self.assertRaises(server.Invalid):
            server._v_path("/etc/passwd", "scan_roots")

    def test_rejects_empty_and_non_text(self):
        for bad in ("", "   ", 5, None):
            with self.assertRaises(server.Invalid):
                server._v_path(bad, "scan_roots")

    def test_deduplicates_case_insensitively(self):
        got = server._v_paths(["D:\\Games", "d:\\games", "E:\\Games"], "scan_roots")
        self.assertEqual(got, ["D:\\Games", "E:\\Games"])

    def test_rejects_a_string_where_a_list_belongs(self):
        with self.assertRaises(server.Invalid):
            server._v_paths("D:\\Games", "scan_roots")


class ScalarValidatorTest(unittest.TestCase):
    def test_port_range(self):
        self.assertEqual(server._v_port("8777", "port"), 8777)
        for bad in (80, 0, 70000, "abc"):
            with self.assertRaises(server.Invalid):
                server._v_port(bad, "port")

    def test_browser_is_an_enum(self):
        self.assertEqual(server._v_browser("edge", "browser"), "edge")
        with self.assertRaises(server.Invalid):
            server._v_browser("netscape", "browser")

    def test_steam_id_is_digits(self):
        self.assertEqual(server._v_steam_id("76561198000000000", "steam_id"),
                         "76561198000000000")
        self.assertEqual(server._v_steam_id("", "steam_id"), "")
        with self.assertRaises(server.Invalid):
            server._v_steam_id("not-an-id", "steam_id")

    def test_secret_rejects_junk_but_allows_empty(self):
        self.assertEqual(server._v_secret("", "steam_api_key"), "")
        self.assertEqual(server._v_secret("AbC-123_x.y", "steam_api_key"), "AbC-123_x.y")
        with self.assertRaises(server.Invalid):
            server._v_secret("has spaces", "steam_api_key")

    def test_bool_is_strict(self):
        self.assertTrue(server._v_bool(True, "include_owned"))
        with self.assertRaises(server.Invalid):
            server._v_bool("yes", "include_owned")


class ExeOverrideTest(unittest.TestCase):
    """An exe supplied over HTTP must stay inside the game's own folder.

    Without this, POSTing an absolute path to /api/override and then /api/launch runs
    any binary on the machine -- and any page in the browser can make both calls.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.install = os.path.join(self.tmp, "MyGame")
        os.makedirs(self.install)
        self.exe = os.path.join(self.install, "MyGame.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"MZ")
        self.outside = os.path.join(self.tmp, "evil.exe")
        with open(self.outside, "wb") as fh:
            fh.write(b"MZ")
        self.game = {"id": "folder:test", "install_dir": self.install}

    def test_accepts_an_exe_inside_the_game_folder(self):
        ok, value = server._validate_exe(self.game, self.exe)
        self.assertTrue(ok, value)

    def test_rejects_an_exe_outside_the_game_folder(self):
        ok, err = server._validate_exe(self.game, self.outside)
        self.assertFalse(ok)
        self.assertIn("inside the game folder", err)

    def test_rejects_traversal_out_of_the_game_folder(self):
        ok, err = server._validate_exe(self.game, os.path.join("..", "evil.exe"))
        self.assertFalse(ok)

    def test_rejects_a_non_exe(self):
        ok, err = server._validate_exe(self.game, os.path.join(self.install, "readme.txt"))
        self.assertFalse(ok)
        self.assertIn(".exe", err)

    def test_rejects_a_missing_file(self):
        ok, err = server._validate_exe(self.game, os.path.join(self.install, "gone.exe"))
        self.assertFalse(ok)

    def test_rejects_when_the_game_has_no_folder(self):
        ok, err = server._validate_exe({"id": "x", "install_dir": None}, self.exe)
        self.assertFalse(ok)


class ImageSniffTest(unittest.TestCase):
    """Cover art may live anywhere the user likes, so the constraint is what the file
    is, not where it sits. Extension alone would let config.json be renamed to .jpg."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_accepts_real_image_headers(self):
        for name, magic in [("a.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 8),
                            ("a.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 4),
                            ("a.gif", b"GIF89a" + b"\0" * 6),
                            ("a.bmp", b"BM" + b"\0" * 10)]:
            self.assertTrue(server._is_image_file(self.write(name, magic)), name)

    def test_accepts_webp(self):
        path = self.write("a.webp", b"RIFF" + b"\0\0\0\0" + b"WEBP")
        self.assertTrue(server._is_image_file(path))

    def test_rejects_a_config_file_renamed_to_jpg(self):
        path = self.write("config.py.jpg", b'{"steam_api_key": "secret"}')
        self.assertFalse(server._is_image_file(path))

    def test_rejects_a_missing_file(self):
        self.assertFalse(server._is_image_file(os.path.join(self.tmp, "nope.jpg")))


class ContainmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_sibling_prefix_is_not_containment(self):
        """The old check was startswith, which 'web-backup' satisfies for 'web'."""
        web = os.path.join(self.tmp, "web")
        sibling = os.path.join(self.tmp, "web-backup")
        os.makedirs(web)
        os.makedirs(sibling)
        self.assertTrue(server._within(web, os.path.join(web, "app.js")))
        self.assertFalse(server._within(web, os.path.join(sibling, "app.js")))


if __name__ == "__main__":
    unittest.main()
