#!/usr/bin/env python3
"""Tests for mcp/brand_focus_race.py against tests/mock_compositor.py.

No real compositor is needed: every test drives the race with an explicitly
injected mock runner/state, or via BRAND_MOCK_COMPOSITOR=1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for _name, _file in (
    ("hypr_management", ROOT / "mcp" / "hypr_management.py"),
    ("brand_focus_race", ROOT / "mcp" / "brand_focus_race.py"),
    ("mock_compositor", ROOT / "tests" / "mock_compositor.py"),
):
    _spec = importlib.util.spec_from_file_location(_name, str(_file))
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)

hypr_management = sys.modules["hypr_management"]
brand_focus_race = sys.modules["brand_focus_race"]
mock_compositor = sys.modules["mock_compositor"]

MockCompositor = mock_compositor.MockCompositor
FocusRaceError = brand_focus_race.FocusRaceError
focus_window_race = brand_focus_race.focus_window_race

FAST = {"dispatch_timeout": 1.0, "verify_timeout": 0.4, "race_timeout": 5.0}


def _window(address, pid="4242", app_class="kitty", start="100001"):
    return {
        "address": address,
        "pid": pid,
        "class": app_class,
        "initialClass": app_class,
        "processStartTime": start,
        "title": app_class,
        "workspace": {"id": 1, "name": "1"},
    }


def _make_mock(**kwargs):
    kwargs.setdefault("clients", [_window("0xaaa1"), _window("0xbbb2", "4343", "firefox", "100002")])
    kwargs.setdefault("active_address", "0xaaa1")
    return MockCompositor(**kwargs)


class FocusRaceTest(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _race(self, mock, address="0xbbb2", **kwargs):
        params = dict(FAST)
        params.update(kwargs)
        return focus_window_race(
            address, runner=mock.runner, state=mock.state_client(), **params
        )

    # -- winner selection -------------------------------------------------
    def test_race_picks_the_verified_winner(self):
        mock = _make_mock(behavior={"plugin": False, "wlrctl": False, "legacy": True})
        winner, argv = self._race(mock)
        self.assertEqual(winner, "legacy")
        self.assertEqual(argv[:3], ("hyprctl", "dispatch", "focuswindow"))
        self.assertEqual(mock.active_address, "0xbbb2")

    def test_all_success_prefers_plugin_on_same_tick(self):
        mock = _make_mock()
        winner, argv = self._race(mock)
        self.assertEqual(winner, "plugin")
        self.assertEqual(argv[:3], ("hyprctl", "dispatch", "brand:manage"))
        self.assertIn("focus,", argv[3])

    def test_wlrctl_wins_when_plugin_and_legacy_lose(self):
        mock = _make_mock(behavior={"plugin": False, "wlrctl": True, "legacy": False})
        winner, argv = self._race(mock)
        self.assertEqual(winner, "wlrctl")
        self.assertEqual(argv[:3], ("wlrctl", "toplevel", "focus"))

    # -- strict verification ----------------------------------------------
    def test_bare_ok_without_focus_movement_loses(self):
        mock = _make_mock(behavior={"plugin": False, "wlrctl": False, "legacy": False})
        with self.assertRaises(FocusRaceError) as ctx:
            self._race(mock)
        message = str(ctx.exception)
        self.assertIn("plugin", message)
        self.assertIn("wlrctl", message)
        self.assertIn("legacy", message)
        self.assertIn("never showed", message)
        # nothing actually moved
        self.assertEqual(mock.active_address, "0xaaa1")

    def test_all_paths_fail_aggregates_errors(self):
        mock = _make_mock(behavior={"plugin": "error", "wlrctl": "error", "legacy": "error"})
        with self.assertRaises(FocusRaceError) as ctx:
            self._race(mock)
        message = str(ctx.exception)
        for name in ("plugin", "wlrctl", "legacy"):
            self.assertIn(name, message)

    # -- skips --------------------------------------------------------------
    def test_plugin_disabled_falls_back_to_wlrctl(self):
        os.environ["BRAND_DISABLE_PLUGIN"] = "1"
        mock = _make_mock()
        winner, _argv = self._race(mock)
        self.assertEqual(winner, "wlrctl")
        dispatched = [c for c in mock.calls if c[1:3] == ("dispatch", "brand:manage")]
        self.assertEqual(dispatched, [])

    def test_plugin_not_loaded_skips_plugin(self):
        mock = _make_mock(plugin_loaded=False)
        winner, _argv = self._race(mock)
        self.assertEqual(winner, "wlrctl")

    def test_plugin_disabled_all_fail_mentions_skip(self):
        os.environ["BRAND_DISABLE_PLUGIN"] = "1"
        mock = _make_mock(behavior={"wlrctl": "error", "legacy": "error"})
        with self.assertRaises(FocusRaceError) as ctx:
            self._race(mock)
        self.assertIn("plugin: skipped (BRAND_DISABLE_PLUGIN=1)", str(ctx.exception))

    # -- dry run -------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        os.environ["BRAND_DRY_RUN"] = "1"
        mock = _make_mock()
        winner, argv = self._race(mock)
        self.assertEqual(winner, "dry-run")
        self.assertEqual(argv, ())
        mutating = [c for c in mock.calls if c[0] in ("wlrctl",) or (len(c) > 2 and c[1] == "dispatch")]
        self.assertEqual(mutating, [])
        self.assertEqual(mock.active_address, "0xaaa1")

    # -- mock env wiring -------------------------------------------------------
    def test_mock_compositor_env_wiring(self):
        os.environ["BRAND_MOCK_COMPOSITOR"] = "1"
        os.environ["BRAND_MOCK_STATE"] = json.dumps(
            {
                "clients": [_window("0xaaa1"), _window("0xbbb2", "4343", "firefox", "100002")],
                "active": "0xaaa1",
                "behavior": {"plugin": True, "wlrctl": True, "legacy": True},
            }
        )
        winner, _argv = focus_window_race("0xbbb2", **FAST)
        self.assertEqual(winner, "plugin")

    # -- HyprManagement.focus() wiring ------------------------------------------
    def _management(self, mock):
        return hypr_management.HyprManagement(runner=mock.runner, state=mock.state_client())

    def test_focus_success_end_to_end(self):
        mock = _make_mock()
        result = self._management(mock).focus("0xbbb2")
        self.assertTrue(result.changed)
        self.assertTrue(result.verified)
        self.assertEqual(result.action, "focus")
        self.assertEqual(len(result.commands), 1)
        self.assertEqual(mock.active_address, "0xbbb2")

    def test_focus_already_focused_is_noop(self):
        mock = _make_mock()
        result = self._management(mock).focus("0xaaa1")
        self.assertFalse(result.changed)
        self.assertTrue(result.verified)
        self.assertEqual(result.commands, ())
        mutating = [c for c in mock.calls if len(c) > 2 and c[1] == "dispatch"]
        self.assertEqual(mutating, [])

    def test_stale_target_refused(self):
        mock = _make_mock()
        real_runner = mock.runner
        calls = {"n": 0}

        def recycling_runner(argv):
            argv = tuple(argv)
            if argv[:3] == ("hyprctl", "-j", "clients"):
                calls["n"] += 1
                if calls["n"] >= 2:
                    # the window was recycled: same address, different pid
                    clients = [dict(c, pid="9999") for c in mock._clients]
                    return mock_compositor.CommandResult(0, json.dumps(clients), "")
            return real_runner(argv)

        management = hypr_management.HyprManagement(
            runner=recycling_runner,
            state=hypr_management.HyprctlStateClient(recycling_runner),
        )
        with self.assertRaises(hypr_management.StaleTarget):
            management.focus("0xbbb2")

    def test_focus_all_fail_raises_verification_failed(self):
        mock = _make_mock(behavior={"plugin": "error", "wlrctl": "error", "legacy": "error"})
        with self.assertRaises(hypr_management.VerificationFailed) as ctx:
            self._management(mock).focus("0xbbb2")
        self.assertIn("plugin", str(ctx.exception))

    def test_focus_dry_run_reports_no_change(self):
        os.environ["BRAND_DRY_RUN"] = "1"
        mock = _make_mock()
        result = self._management(mock).focus("0xbbb2")
        self.assertFalse(result.changed)
        self.assertFalse(result.verified)
        self.assertEqual(result.commands, ())
        self.assertEqual(mock.active_address, "0xaaa1")

    def test_focus_unknown_address_raises_target_not_found(self):
        mock = _make_mock()
        with self.assertRaises(hypr_management.TargetNotFound):
            self._management(mock).focus("0xdead")


if __name__ == "__main__":
    unittest.main()
