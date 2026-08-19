"""The executable heuristic, tested against trees we build rather than a real library.

scan.py --selftest used to assert these against one specific machine, with hardcoded
ids like "folder:GVALORANT". Those literals never matched what folders.folder_id()
actually produces ("folder:G--VALORANT"), so the checks silently passed for the entire
life of the project while testing nothing. These assert the same behaviour, on any
machine, in a temp directory.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import exefind  # noqa: E402


def _touch(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


class ExeFindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def game(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    # -- the decoy gate: this is what keeps clip folders out of the library --

    def test_folder_with_no_executable_is_not_a_game(self):
        """Clip recorders create a folder per game containing only video."""
        folder = self.game("VALORANT")
        for clip in ("clip1.mp4", "clip2.mp4", "thumb.png"):
            _touch(os.path.join(folder, clip))
        self.assertIsNone(exefind.pick(folder, "VALORANT"))

    def test_empty_folder_is_not_a_game(self):
        self.assertIsNone(exefind.pick(self.game("Empty"), "Empty"))

    # -- rejects: the uninstaller must never win --

    def test_uninstaller_loses_to_the_real_binary(self):
        folder = self.game("Stray")
        _touch(os.path.join(folder, "Stray.exe"), 40_000_000)
        _touch(os.path.join(folder, "unins000.exe"), 900_000)
        self.assertEqual(exefind.pick(folder, "Stray")["name"], "Stray.exe")

    def test_uninstaller_alone_is_not_a_game(self):
        folder = self.game("Leftovers")
        _touch(os.path.join(folder, "unins000.exe"))
        self.assertIsNone(exefind.pick(folder, "Leftovers"))

    def test_redistributables_are_rejected(self):
        folder = self.game("SomeGame")
        _touch(os.path.join(folder, "_CommonRedist", "vcredist_x64.exe"))
        _touch(os.path.join(folder, "DXSETUP.exe"))
        self.assertIsNone(exefind.pick(folder, "SomeGame"))

    # -- depth: the retry that finds Unreal-style repacks --

    def test_deep_unreal_style_binary_is_found_on_retry(self):
        folder = self.game("Repack")
        _touch(os.path.join(folder, "Repack", "Gameface", "Binaries",
                            "Win64", "Repack.exe"), 30_000_000)
        found = exefind.pick(folder, "Repack")
        self.assertIsNotNone(found, "depth-6 retry should reach the buried binary")
        self.assertEqual(found["name"], "Repack.exe")

    def test_name_similarity_beats_a_bundled_tool(self):
        folder = self.game("Cyberpunk 2077")
        _touch(os.path.join(folder, "Cyberpunk2077.exe"), 50_000_000)
        _touch(os.path.join(folder, "tools", "REDprelauncher.exe"), 2_000_000)
        self.assertEqual(exefind.pick(folder, "Cyberpunk 2077")["name"],
                         "Cyberpunk2077.exe")


class DepthTest(unittest.TestCase):
    """_depth counted forward slashes only, so on Windows every path measured 0 and the
    max_depth prune never fired: each game folder was walked to the bottom."""

    def test_depth_counts_both_separators(self):
        self.assertEqual(exefind._depth("/mnt/e/Games/Stray"), 4)
        self.assertEqual(exefind._depth("E:\\Games\\Stray"), 2)
        self.assertEqual(exefind._depth("E:/Games/Stray"), 2)

    def test_depth_ignores_a_trailing_separator(self):
        self.assertEqual(exefind._depth("E:\\Games\\"), exefind._depth("E:\\Games"))


class QuickProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)

    def test_probe_sees_a_shallow_executable(self):
        folder = os.path.join(self.tmp, "Game")
        _touch(os.path.join(folder, "Game.exe"))
        self.assertTrue(exefind.quick_probe(folder))

    def test_probe_rejects_a_clip_folder(self):
        folder = os.path.join(self.tmp, "Clips")
        _touch(os.path.join(folder, "a.mp4"))
        self.assertFalse(exefind.quick_probe(folder))

    def test_probe_ignores_an_uninstaller(self):
        folder = os.path.join(self.tmp, "Stale")
        _touch(os.path.join(folder, "unins000.exe"))
        self.assertFalse(exefind.quick_probe(folder))

    def test_probe_does_not_recurse_forever(self):
        """Bounded by design: it runs across every folder on every drive."""
        deep = os.path.join(self.tmp, "Deep", *[f"l{i}" for i in range(8)])
        _touch(os.path.join(deep, "Buried.exe"))
        self.assertFalse(exefind.quick_probe(os.path.join(self.tmp, "Deep")))

    def test_probe_on_a_missing_folder_is_false(self):
        self.assertFalse(exefind.quick_probe(os.path.join(self.tmp, "nope")))


if __name__ == "__main__":
    unittest.main()
