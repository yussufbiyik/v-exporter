"""Tests for the ``--selftest`` exit code and its JSON ``ok`` field.

These tests exercise ``pipeline.run_selftest`` with the environment-dependent
calls mocked out, so they run without WolvenKit, Blender, the game installed or
a real settings file. The goal is to pin the exit-code contract:

* every check passes -> return 0
* any check fails    -> return 1

The CLI wrapper (``cli._cmd_selftest``) must expose the same result as both its
exit code and its JSON ``ok`` value.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import cli
import pipeline as pl


class _DummyLogger:
    """Logger stand-in that swallows output instead of writing a log file."""

    def info(self, message):  # noqa: D401 - test double
        pass

    def step(self, message):
        pass

    def warn(self, message):
        pass

    def error(self, message):
        pass

    def raw(self, message):
        pass

    def close(self):
        pass


class SelftestExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vx-selftest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # run_selftest reports a previous settings.json parse error through a
        # module global; reset it so the pass case is deterministic.
        pl._last_settings_error = None

        self.logger = _DummyLogger()

        # A real, compilable helper script so the "blender_export.py" check
        # passes without touching the repository itself.
        self.helper_script = Path(self.tmp) / "blender_export.py"
        self.helper_script.write_text("value = 1\n", encoding="utf-8")

        # `run_selftest` imports tkinter; inject a stub so the test does not
        # depend on the host's Tk installation.
        fake_tk = types.ModuleType("tkinter")
        fake_tk.TkVersion = 8.6
        self._tk_patch = mock.patch.dict(sys.modules, {"tkinter": fake_tk})
        self._tk_patch.start()
        self.addCleanup(self._tk_patch.stop)

    def _settings(self, blender_path: str) -> pl.Settings:
        settings = pl.Settings()
        settings.wolvenkit_cli = str(self.helper_script)  # a real file
        settings.blender = blender_path
        settings.game_root = self.tmp
        settings.npv_root = self.tmp
        settings.output_root = self.tmp
        return settings

    def _environment_patches(self):
        npv = pl.NpvInfo(
            name="TestNPV",
            directory=Path(self.tmp),
            archives=[Path(self.tmp) / "TestNPV.archive"],
        )
        return [
            mock.patch.object(pl, "normalize_game_root", return_value=Path(self.tmp)),
            mock.patch.object(pl, "effective_npv_root", return_value=Path(self.tmp)),
            mock.patch.object(pl, "list_npvs", return_value=[npv]),
            mock.patch.object(
                pl,
                "probe_wolvenkit",
                return_value={
                    "ok": True,
                    "code": 0,
                    "version": (10, 0, 0),
                    "output": "10.0.0",
                },
            ),
            mock.patch.object(
                pl,
                "probe_blender",
                return_value={
                    "ok": True,
                    "version": (5, 0, 0),
                    "version_string": "5.0",
                },
            ),
            mock.patch.object(
                pl,
                "probe_blender_addons",
                return_value={
                    "payload": {
                        "addons": ["i_scene_cp77_gltf"],
                        "enabled": ["i_scene_cp77_gltf"],
                    }
                },
            ),
            mock.patch.object(pl, "BLENDER_SCRIPT", self.helper_script),
        ]

    def _run_with_environment(self, settings: pl.Settings) -> int:
        with contextlib.ExitStack() as stack:
            for patcher in self._environment_patches():
                stack.enter_context(patcher)
            return pl.run_selftest(settings, self.logger)

    def test_returns_zero_when_all_checks_pass(self):
        settings = self._settings(str(self.helper_script))
        self.assertEqual(self._run_with_environment(settings), 0)

    def test_returns_one_when_a_check_fails(self):
        missing_blender = str(Path(self.tmp) / "missing" / "blender.exe")
        settings = self._settings(missing_blender)
        self.assertEqual(self._run_with_environment(settings), 1)

    def test_cli_json_ok_field_matches_exit_code(self):
        args = types.SimpleNamespace(
            wolvenkit_cli="set",
            blender="set",
            game_root="set",
            npv_root="set",
            output_root="set",
            json=True,
        )
        settings = pl.Settings()
        for expected_code, expected_ok in ((0, True), (1, False)):
            with self.subTest(exit_code=expected_code):
                buffer = io.StringIO()
                with mock.patch.object(
                    cli.pl, "run_selftest", return_value=expected_code
                ), redirect_stdout(buffer):
                    code = cli._cmd_selftest(args, settings, self.logger)
                payload = json.loads(buffer.getvalue())
                self.assertEqual(code, expected_code)
                self.assertEqual(payload["exit_code"], expected_code)
                self.assertEqual(payload["ok"], expected_ok)


if __name__ == "__main__":
    unittest.main()
