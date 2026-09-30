#!/usr/bin/env python3
"""Mock compositor for brand focus-race testing (BRAND_MOCK_COMPOSITOR=1).

Simulates ``hyprctl`` JSON responses, ``hyprctl plugin list``, ``wlrctl``,
and the native ``brand:manage`` dispatch so :func:`focus_window_race` is
testable with no real compositor.  Per-strategy behavior is scriptable:

  True    -- the dispatch moves focus to the requested window (success)
  False   -- the dispatch answers "ok" but does NOT move focus (bare-ok loss)
  "error" -- the dispatch itself fails

``MockCompositor.from_env()`` builds one from the ``BRAND_MOCK_STATE`` JSON
fixture (``{"clients": [...], "active": "0x...", "behavior": {...},
"plugin_loaded": true}``); with no fixture it defaults to two windows where
every strategy moves focus.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Mapping, Sequence


def _load_hypr_management() -> Any:
    existing = sys.modules.get("hypr_management")
    if existing is not None:
        return existing
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hypr_management", str(root / "mcp" / "hypr_management.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load mcp/hypr_management.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_hm = _load_hypr_management()
CommandResult = _hm.CommandResult


def _bare(address: Any) -> str:
    text = str(address or "").strip().lower()
    if text.startswith("address:"):
        text = text[len("address:") :]
    return text.split("@", 1)[0]


class MockCompositor:
    """Scriptable stand-in for hyprctl/wlrctl/brandctl."""

    def __init__(
        self,
        *,
        clients: Sequence[Mapping[str, Any]] = (),
        active_address: Any = None,
        behavior: Mapping[str, Any] | None = None,
        plugin_loaded: bool = True,
    ) -> None:
        self._clients = [dict(client) for client in clients]
        self._active = _bare(active_address) if active_address else ""
        merged = {"plugin": True, "wlrctl": True, "legacy": True}
        if behavior:
            merged.update(behavior)
        self._behavior = merged
        self._plugin_loaded = plugin_loaded
        self.calls: list[tuple[str, ...]] = []
        self._lock = threading.Lock()

    # -- construction ----------------------------------------------------
    @classmethod
    def default(cls) -> "MockCompositor":
        """Two windows, first active, every strategy moves focus."""
        return cls(
            clients=[_window("0xaaa1", "4242", "kitty"), _window("0xbbb2", "4343", "firefox")],
            active_address="0xaaa1",
        )

    @classmethod
    def from_env(cls) -> "MockCompositor":
        raw = os.environ.get("BRAND_MOCK_STATE", "").strip()
        if not raw:
            return cls.default()
        data = json.loads(raw)
        return cls(
            clients=data.get("clients", ()),
            active_address=data.get("active"),
            behavior=data.get("behavior"),
            plugin_loaded=data.get("plugin_loaded", True),
        )

    # -- runner / state client -------------------------------------------
    def runner(self, argv: Sequence[str]) -> Any:
        argv = tuple(str(part) for part in argv)
        with self._lock:
            self.calls.append(argv)
        if not argv:
            return CommandResult(1, "", "mock: empty argv")
        head = argv[0]
        if head == "hyprctl" and argv[1:3] == ("-j", "clients"):
            return CommandResult(0, json.dumps(self._clients), "")
        if head == "hyprctl" and argv[1:3] == ("-j", "activewindow"):
            window = self._find(self._active)
            return CommandResult(0, json.dumps(window) if window else "{}", "")
        if head == "hyprctl" and argv[1:3] == ("plugin", "list"):
            text = "Plugin libbrand.so:\n  by toxicwind\n  version: 0.4.0" if self._plugin_loaded else "(no plugins loaded)"
            return CommandResult(0, text, "")
        if head == "hyprctl" and argv[1:3] == ("dispatch", "brand:manage") and len(argv) == 4:
            action, _, rest = argv[3].partition(",")
            if action.strip().lower() != "focus":
                return CommandResult(1, "", f"mock: unsupported manage action {action!r}")
            return self._apply("plugin", rest)
        if head == "wlrctl" and argv[1:3] == ("toplevel", "focus") and len(argv) == 4:
            return self._apply_wlrctl(argv[3])
        if head == "hyprctl" and argv[1:3] == ("dispatch", "focuswindow") and len(argv) == 4:
            return self._apply("legacy", argv[3])
        return CommandResult(1, "", f"mock: unhandled {argv!r}")

    def state_client(self) -> Any:
        return _hm.HyprctlStateClient(self.runner)

    # -- scripted behavior ------------------------------------------------
    def _find(self, bare: str) -> dict[str, Any] | None:
        for client in self._clients:
            if _bare(client.get("address")) == bare and bare:
                return dict(client)
        return None

    def _apply(self, strategy: str, selector: Any) -> Any:
        """Apply one strategy's scripted behavior to the mock focus state."""
        mode = self._behavior.get(strategy, True)
        if mode == "error":
            return CommandResult(1, "", f"mock {strategy} dispatch failed")
        if mode is True:
            with self._lock:
                self._active = _bare(selector)
        # False: bare "ok" that never moves focus -- the strict-loss case.
        return CommandResult(0, "ok", "")

    def _apply_wlrctl(self, app_id: str) -> Any:
        for client in self._clients:
            if str(client.get("class") or "") == app_id:
                return self._apply("wlrctl", client.get("address"))
        return CommandResult(1, "", f"mock wlrctl: no toplevel with app-id {app_id!r}")

    # -- test helpers ------------------------------------------------------
    @property
    def active_address(self) -> str:
        with self._lock:
            return self._active

    def set_clients(self, clients: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            self._clients = [dict(client) for client in clients]

    def reset_calls(self) -> None:
        with self._lock:
            self.calls.clear()


def _window(address: str, pid: str, app_class: str, **extra: Any) -> dict[str, Any]:
    window = {
        "address": address,
        "pid": pid,
        "class": app_class,
        "initialClass": app_class,
        "processStartTime": "100001",
        "title": app_class,
        "workspace": {"id": 1, "name": "1"},
        "mapped": True,
        "hidden": False,
    }
    window.update(extra)
    return window
