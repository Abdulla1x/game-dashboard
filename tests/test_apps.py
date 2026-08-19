"""Manually added apps and companion launches.

The add route is the first place an HTTP request names a program the scanners never
found. `_guard` is what keeps another origin from reaching it at all; the validators
here decide how much a bug in that guard would be worth, so they are asserted rather
than trusted. Fixtures are built in a temp directory so this runs on Linux too.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan  # noqa: E402
import server  # noqa: E402


class TargetValidatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, data=b"MZ"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_accepts_an_executable_that_exists(self):
        exe = self.write("Game.exe")
        self.assertEqual(server._v_target(exe), ("exe", exe, {}))

    def test_rejects_an_executable_that_does_not_exist(self):
        with self.assertRaises(server.Invalid):
            server._v_target(os.path.join(self.tmp, "gone.exe"))

    def test_rejects_a_script_or_an_unknown_extension(self):
        for name in ("run.bat", "run.cmd", "run.ps1", "notes.txt", "plain"):
            self.write(name)
            with self.assertRaises(server.Invalid, msg=name):
                server._v_target(os.path.join(self.tmp, name))

    def test_rejects_a_relative_path(self):
        with self.assertRaises(server.Invalid):
            server._v_target("Game.exe")

    def test_rejects_traversal(self):
        with self.assertRaises(server.Invalid):
            server._v_target("C:\\Games\\..\\Windows\\System32\\cmd.exe")

    def test_rejects_the_device_namespace(self):
        for bad in ("\\\\?\\C:\\Windows\\System32\\cmd.exe", "\\\\.\\pipe\\x.exe"):
            with self.assertRaises(server.Invalid, msg=bad):
                server._v_target(bad)

    def test_rejects_a_target_inside_the_windows_directory(self):
        """One rule instead of a list of every borrowable System32 binary."""
        fake_windows = os.path.join(self.tmp, "Windows")
        os.makedirs(os.path.join(fake_windows, "System32"))
        exe = self.write(os.path.join("Windows", "System32", "cmd.exe"))
        original = server._SYSTEM_ROOT
        server._SYSTEM_ROOT = fake_windows
        self.addCleanup(setattr, server, "_SYSTEM_ROOT", original)
        with self.assertRaises(server.Invalid):
            server._v_target(exe)

    def test_rejects_embedded_control_characters_and_quotes(self):
        """Surrounding whitespace is trimmed; a character *inside* the value is not."""
        exe = self.write("Game.exe")
        for bad in ("C:\\Games\\a\nb.exe", "C:\\Games\\a\x00b.exe", '"' + exe + '"'):
            with self.assertRaises(server.Invalid, msg=repr(bad)):
                server._v_target(bad)

    def test_accepts_allowed_url_schemes(self):
        for url in ("steam://rungameid/570",
                    "com.epicgames.launcher://apps/x?action=launch",
                    "https://tracker.gg/valorant",
                    "http://127.0.0.1:3000/"):
            self.assertEqual(server._v_target(url), ("url", url, {}))

    def test_rejects_every_other_scheme(self):
        for bad in ("file:///C:/Windows/System32/cmd.exe", "javascript:alert(1)",
                    "data:text/html,x", "shell:AppsFolder\\X", "ms-msdt:/id",
                    "mailto:a@b.c", "vbscript:x", "steam:/rungameid/570"):
            with self.assertRaises(server.Invalid, msg=bad):
                server._v_target(bad)

    def test_rejects_empty_and_non_text(self):
        for bad in ("", "   ", 5, None, ["C:\\x.exe"]):
            with self.assertRaises(server.Invalid):
                server._v_target(bad)


class ShortcutIconTest(unittest.TestCase):
    """A launcher-hosted app ships its artwork as a loose .ico beside the launcher."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exe = os.path.join(self.tmp, "Launcher.exe")
        with open(self.exe, "wb") as fh:
            fh.write(b"MZ")
        self.ico = os.path.join(self.tmp, "App.ico")
        with open(self.ico, "wb") as fh:
            fh.write(b"\x00\x00\x01\x00")

    def test_keeps_a_separate_icon_file(self):
        self.assertEqual(server._icon_beside(self.ico + ",0", self.exe), self.ico)

    def test_ignores_an_icon_that_is_just_the_target(self):
        """art.py would have found the exe's own icon anyway."""
        self.assertEqual(server._icon_beside(self.exe + ",0", self.exe), "")

    def test_ignores_a_missing_or_unreadable_icon(self):
        for bad in ("", None, os.path.join(self.tmp, "gone.ico"),
                    os.path.join(self.tmp, "App.png")):
            self.assertEqual(server._icon_beside(bad, self.exe), "")


class ArgsValidatorTest(unittest.TestCase):
    """Windows arguments are mostly backslashes, which is what breaks both shlex presets."""

    def test_keeps_backslashes_outside_quotes(self):
        self.assertEqual(server._v_args(r"-Djava.library.path=C:\mc\natives"),
                         [r"-Djava.library.path=C:\mc\natives"])

    def test_handles_a_quote_in_the_middle_of_a_token(self):
        """posix=False plus stripping outer quotes splits this into two wrong tokens."""
        self.assertEqual(server._v_args(r'--dir="C:\Program Files\X" -v'),
                         [r"--dir=C:\Program Files\X", "-v"])

    def test_handles_a_whole_quoted_token(self):
        self.assertEqual(server._v_args(r'--dir "C:\Program Files\X" -v'),
                         [r"--dir", r"C:\Program Files\X", "-v"])

    def test_hash_is_not_a_comment(self):
        self.assertEqual(server._v_args("--tag #1234"), ["--tag", "#1234"])

    def test_empty_is_no_arguments(self):
        for empty in (None, "", []):
            self.assertEqual(server._v_args(empty), [])

    def test_rejects_an_unclosed_quote(self):
        with self.assertRaises(server.Invalid):
            server._v_args('--dir "C:\\x')

    def test_caps_count_and_length(self):
        with self.assertRaises(server.Invalid):
            server._v_args(" ".join(["-a"] * 33))
        with self.assertRaises(server.Invalid):
            server._v_args("x" * 1025)

    def test_rejects_non_text(self):
        with self.assertRaises(server.Invalid):
            server._v_args(5)


class AppNameTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(server._v_app_name("  Modrinth   App "), "Modrinth App")

    def test_rejects_empty_and_over_long(self):
        for bad in ("", "   ", "x" * 121, 5, None):
            with self.assertRaises(server.Invalid):
                server._v_app_name(bad)


class ManualIdTest(unittest.TestCase):
    def test_slugs_the_name(self):
        self.assertEqual(server._manual_id("Modrinth App", set()), "manual:modrinth-app")
        self.assertEqual(server._manual_id("Valorant Tracker!!", set()),
                         "manual:valorant-tracker")

    def test_suffixes_on_collision(self):
        taken = {"manual:modrinth-app", "manual:modrinth-app-2"}
        self.assertEqual(server._manual_id("Modrinth App", taken), "manual:modrinth-app-3")

    def test_falls_back_when_the_name_does_not_slug(self):
        got = server._manual_id("日本語", set())
        self.assertTrue(got.startswith("manual:app-"), got)


class CompanionValidatorTest(unittest.TestCase):
    def setUp(self):
        self.original = server.STATE["library"]
        server.STATE["library"] = {"games": [
            {"id": "manual:tracker", "name": "Tracker"},
            {"id": "shortcut:discord", "name": "Discord"},
        ]}
        self.addCleanup(server.STATE.__setitem__, "library", self.original)

    def test_accepts_known_ids_and_dedupes_in_order(self):
        got = server._v_companions(
            ["shortcut:discord", "manual:tracker", "shortcut:discord"], "riot:valorant")
        self.assertEqual(got, ["shortcut:discord", "manual:tracker"])

    def test_rejects_an_unknown_id(self):
        with self.assertRaises(server.Invalid):
            server._v_companions(["manual:nope"], "riot:valorant")

    def test_keeps_an_id_that_is_already_stored(self):
        """An offline drive must not silently delete a companion the user still wants."""
        got = server._v_companions(["folder:Z--Game"], "riot:valorant",
                                   already=["folder:Z--Game"])
        self.assertEqual(got, ["folder:Z--Game"])

    def test_rejects_self_reference(self):
        with self.assertRaises(server.Invalid):
            server._v_companions(["riot:valorant"], "riot:valorant")

    def test_rejects_a_non_list_and_an_over_long_list(self):
        with self.assertRaises(server.Invalid):
            server._v_companions("manual:tracker", "riot:valorant")
        with self.assertRaises(server.Invalid):
            server._v_companions(["manual:tracker"] * 9, "riot:valorant")

    def test_empty_is_allowed(self):
        self.assertEqual(server._v_companions([], "riot:valorant"), [])


class BlankRecordTest(unittest.TestCase):
    """extra_games has always accepted `exe`; adding `args` and `url` must not move it."""

    def test_the_documented_shape_is_unchanged(self):
        got = scan._blank_record({"id": "manual:x", "name": "X", "exe": "D:\\g\\x.exe"})
        self.assertEqual(got["launch"], {"kind": "exe", "value": "D:\\g\\x.exe"})
        self.assertEqual(got["exe_path"], "D:\\g\\x.exe")
        self.assertEqual(got["exe_name"], "x.exe")
        self.assertEqual(got["install_dir"], "D:\\g")
        self.assertTrue(got["installed"])
        self.assertEqual(got["source"], "manual")

    def test_args_produce_exe_args(self):
        got = scan._blank_record({"id": "manual:x", "name": "X",
                                  "exe": "D:\\g\\x.exe", "args": ["-a", "b c"]})
        self.assertEqual(got["launch"], {"kind": "exe_args", "value": "D:\\g\\x.exe",
                                         "args": ["-a", "b c"]})

    def test_a_url_entry_has_no_executable(self):
        got = scan._blank_record({"id": "manual:y", "name": "Y",
                                  "url": "steam://rungameid/570"})
        self.assertEqual(got["launch"], {"kind": "url", "value": "steam://rungameid/570"})
        self.assertIsNone(got["exe_path"])
        self.assertIsNone(got["install_dir"])

    def test_no_target_is_still_a_valid_record(self):
        got = scan._blank_record({"id": "manual:z", "name": "Z"})
        self.assertEqual(got["launch"], {"kind": "none", "value": None})

    def test_an_icon_travels_to_the_record(self):
        got = scan._blank_record({"id": "manual:x", "name": "X", "exe": "D:\\g\\x.exe",
                                  "icon": "D:\\g\\App.ico"})
        self.assertEqual(got["icon_path"], "D:\\g\\App.ico")
        self.assertIsNone(scan._blank_record({"id": "manual:y",
                                              "name": "Y"})["icon_path"])

    def test_records_are_flagged_as_user_added(self):
        self.assertTrue(scan._blank_record({"id": "manual:x", "name": "X"})["user_added"])


class ApplyOverridesTest(unittest.TestCase):
    def game(self):
        return {"id": "riot:valorant", "name": "VALORANT", "source": "riot",
                "installed": True, "install_dir": None,
                "launch": {"kind": "none", "value": None}}

    def test_companions_reach_the_record(self):
        game = self.game()
        scan.apply_overrides([game], {"riot:valorant": {"companions": ["manual:t"]}})
        self.assertEqual(game["companions"], ["manual:t"])

    def test_a_non_string_companion_is_dropped(self):
        game = self.game()
        scan.apply_overrides([game], {"riot:valorant": {"companions": ["manual:t", 7]}})
        self.assertEqual(game["companions"], ["manual:t"])

    def test_derived_flags_are_only_ever_set(self):
        """Why the in-place path pops them first; otherwise Unhide needs a rescan."""
        game = self.game()
        scan.apply_overrides([game], {"riot:valorant": {"hidden": True}})
        scan.apply_overrides([game], {})
        self.assertTrue(game["hidden"])


class CompanionLaunchTest(unittest.TestCase):
    def setUp(self):
        self.started = []
        self.original_library = server.STATE["library"]
        self.original_launch = server.launcher.launch
        server.STATE["library"] = {"games": [
            {"id": "manual:tracker", "name": "Tracker",
             "launch": {"kind": "exe", "value": "T.exe"},
             "companions": ["manual:deep"]},
            {"id": "manual:deep", "name": "Deep",
             "launch": {"kind": "exe", "value": "D.exe"}},
            {"id": "manual:broken", "name": "Broken",
             "launch": {"kind": "none", "value": None}},
        ]}
        self.addCleanup(server.STATE.__setitem__, "library", self.original_library)
        self.addCleanup(setattr, server.launcher, "launch", self.original_launch)

        def fake_launch(game):
            if game["id"] == "manual:broken":
                raise server.launcher.LaunchError("nothing to run")
            self.started.append(game["id"])
            return game["launch"]["value"]

        server.launcher.launch = fake_launch

        # launch_companions narrates to the console; keep the test output clean.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_companions_start_and_are_reported(self):
        also, failed = server.launch_companions({"id": "riot:v", "name": "V",
                                                 "companions": ["manual:tracker"]})
        self.assertEqual(self.started, ["manual:tracker"])
        self.assertEqual(also, ["Tracker"])
        self.assertEqual(failed, [])

    def test_a_companions_own_companions_are_not_followed(self):
        server.launch_companions({"id": "riot:v", "name": "V",
                                  "companions": ["manual:tracker"]})
        self.assertNotIn("manual:deep", self.started)

    def test_a_failing_companion_does_not_stop_the_others(self):
        also, failed = server.launch_companions(
            {"id": "riot:v", "name": "V",
             "companions": ["manual:broken", "manual:tracker"]})
        self.assertEqual(also, ["Tracker"])
        self.assertEqual(len(failed), 1)
        self.assertIn("Broken", failed[0])

    def test_a_missing_companion_is_reported_not_dropped(self):
        also, failed = server.launch_companions({"id": "riot:v", "name": "V",
                                                 "companions": ["folder:D--Gone"]})
        self.assertEqual(also, [])
        self.assertIn("folder:D--Gone", failed[0])

    def test_no_companions_is_a_no_op(self):
        self.assertEqual(server.launch_companions({"id": "riot:v", "name": "V"}), ([], []))


class ExtraGamesGuardTest(unittest.TestCase):
    def test_a_malformed_list_is_refused_rather_than_replaced(self):
        self.assertIsNone(server._extra_games({"extra_games": {"id": "manual:x"}}))
        self.assertEqual(server._extra_games({}), [])
        self.assertEqual(server._extra_games({"extra_games": [{"id": "x"}]}), [{"id": "x"}])


if __name__ == "__main__":
    unittest.main()
