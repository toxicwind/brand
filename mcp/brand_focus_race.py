#!/usr/bin/env python3
"""Hyper-race focus actuation for brand.

Races three concurrent actuation paths to move window focus; a path wins ONLY
when ``hyprctl -j activewindow`` strictly confirms the requested address.  A
bare ``ok`` that did not move focus is a LOSS, never a win.

Paths (one daemon thread each):

  A. ``plugin`` -- native ``hyprctl dispatch brand:manage focus,<qualified>``
     (skipped when ``BRAND_DISABLE_PLUGIN=1`` or the brand plugin is not
     loaded).
  B. ``wlrctl`` -- ``wlrctl toplevel focus <app-id>`` via
     foreign-toplevel-management (skipped when the wlrctl binary is missing).
  C. ``legacy`` -- ``hyprctl dispatch focuswindow address:<addr>`` (always
     applicable; the last resort).

Same-tick ties break plugin > wlrctl > legacy; otherwise first-valid-wins.
Ceilings: ~3s dispatch, ~2s verify, ~12s global.  All path errors and skip
reasons aggregate into :class:`FocusRaceError`.

Env knobs:

  BRAND_DISABLE_PLUGIN=1  -- skip the native plugin path (fall back to B/C).
  BRAND_DRY_RUN=1         -- log what would run; change nothing.
  BRAND_MOCK_COMPOSITOR=1 -- drive the race against the mock compositor
                             (tests/mock_compositor.py) instead of a real one;
                             BRAND_MOCK_STATE carries its JSON fixture.

The pattern is adapted from the lasso hyper-race (ranch/lasso focus_window):
concurrent strategies, strict activewindow verification, preference-ordered
same-tick tie-break, per-strategy ceilings, dry-run barrier before any thread
starts.  Naming and structure here are brand-local.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hypr_management import (
    CommandResult,
    HyprManagementError,
    StateClient,
    _coerce_result,
    address_matches,
    bare_address,
    normalize_address,
    qualified_window_target,
    subprocess_runner,
)

log = logging.getLogger("brand.focus")


class FocusRaceError(HyprManagementError):
    """Every focus actuation path failed or was skipped; carries all reasons."""


DRY_RUN_WINNER = "dry-run"

# Dispatch budget per strategy; a hung strategy never blocks the race.
_DISPATCH_TIMEOUT_S = 3.0
# Activewindow polling budget after a strategy claims success.
_VERIFY_TIMEOUT_S = 2.0
_VERIFY_POLL_S = 0.1
# Backstop: the whole race, stragglers included, never exceeds this.
_RACE_TIMEOUT_S = 12.0
# Race loop quantum. Strategies verifying inside the same quantum tie; ties
# break by _STRATEGY_PREFERENCE, otherwise first-valid-wins, period.
_RACE_QUANTUM_S = 0.025
# Same-tick tie-break only: plugin > wlrctl > legacy.
_STRATEGY_PREFERENCE = ("plugin", "wlrctl", "legacy")


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_mock_compositor() -> Any:
    """Load tests/mock_compositor.py by path (tests/ is not a package).

    Reuses the already-imported ``hypr_management`` module when present so the
    spec load inside the mock fixture cannot clobber it.
    """
    root = Path(__file__).resolve().parents[1]
    path = root / "tests" / "mock_compositor.py"
    spec = importlib.util.spec_from_file_location("brand_mock_compositor", str(path))
    if spec is None or spec.loader is None:
        raise FocusRaceError(f"cannot load mock compositor from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MockCompositor


def _default_backing(
    runner: Callable[[Sequence[str]], Any], state: StateClient | None
) -> tuple[Callable[[Sequence[str]], Any], StateClient | None]:
    """Swap in the mock compositor when BRAND_MOCK_COMPOSITOR=1.

    Only applies when the caller left the defaults in place; an explicitly
    injected runner/state always wins.
    """
    if runner is subprocess_runner and state is None and _env_enabled("BRAND_MOCK_COMPOSITOR"):
        mock = load_mock_compositor().from_env()
        return mock.runner, mock.state_client()
    return runner, state


def _native_argv(argv: Sequence[str]) -> list[str]:
    """Mirror subprocess_runner's brandctl rewrite for direct subprocess use.

    ``hyprctl dispatch brand:manage ...`` is provider-aware: it goes through
    ``brandctl manage`` when available, exactly as subprocess_runner does.
    """
    command = list(argv)
    if len(command) == 4 and command[:3] == ["hyprctl", "dispatch", "brand:manage"]:
        source_ctl = Path(__file__).resolve().parents[1] / "scripts" / "brandctl"
        portalctl = shutil.which("brandctl") or (str(source_ctl) if source_ctl.is_file() else "")
        if not portalctl:
            raise FocusRaceError("brandctl is required for provider-aware native management dispatch")
        command = [portalctl, "manage", command[3]]
    return command


def _run_native(
    argv: Sequence[str], dispatch_timeout: float, runner: Callable[[Sequence[str]], Any]
) -> tuple[str, ...]:
    """Run one dispatch with a hard dispatch ceiling; return the argv run.

    The default runner (subprocess_runner) carries a 5s internal timeout, so
    the race runs native dispatches itself to honor the ~3s dispatch ceiling.
    Custom runners (mocks, test doubles) are called directly instead.
    """
    command = tuple(argv)
    if runner is subprocess_runner:
        try:
            proc = subprocess.run(
                _native_argv(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=dispatch_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise FocusRaceError(f"{' '.join(command)}: dispatch timed out after {dispatch_timeout}s") from exc
        except OSError as exc:
            raise FocusRaceError(f"{' '.join(command)}: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "dispatch failed"
            raise FocusRaceError(f"{' '.join(command)}: {detail}")
        return command
    result = _coerce_result(runner(command))
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "dispatch failed"
        raise FocusRaceError(f"{' '.join(command)}: {detail}")
    return command


def _plugin_loaded(runner: Callable[[Sequence[str]], Any]) -> bool:
    """True when the compositor lists the brand plugin as loaded."""
    try:
        result = _coerce_result(runner(("hyprctl", "plugin", "list")))
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return "brand" in (result.stdout or "").lower()


def _app_id_for_address(
    runner: Callable[[Sequence[str]], Any], address: str
) -> str | None:
    """Map a window address to its Wayland app-id (class).

    One fresh re-query on a miss: the clients list can be stale, and declaring
    wlrctl inapplicable on a stale read would forfeit a working strategy.
    """
    wanted = bare_address(address)
    for _ in range(2):
        try:
            result = _coerce_result(runner(("hyprctl", "-j", "clients")))
        except Exception:
            return None
        if result.returncode != 0:
            return None
        try:
            clients = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None
        for client in clients or []:
            if not isinstance(client, Mapping):
                continue
            try:
                if bare_address(client.get("address", "")) == wanted:
                    return client.get("class") or None
            except Exception:
                continue
    return None


def _wlrctl_focus(
    app_id: str,
    runner: Callable[[Sequence[str]], Any],
    dispatch_timeout: float,
) -> tuple[str, ...]:
    """Focus via wlrctl foreign-toplevel-management. Returns the argv run."""
    argv = ("wlrctl", "toplevel", "focus", app_id)
    if runner is subprocess_runner:
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
        env.setdefault("WAYLAND_DISPLAY", "wayland-1")
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=dispatch_timeout, env=env
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise FocusRaceError(f"wlrctl toplevel focus: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "wlrctl failed"
            raise FocusRaceError(f"wlrctl toplevel focus {app_id!r}: {detail}")
        return argv
    result = _coerce_result(runner(argv))
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "wlrctl failed"
        raise FocusRaceError(f"wlrctl toplevel focus {app_id!r}: {detail}")
    return argv


def _verify_focus(state: StateClient, wanted: str, timeout: float) -> bool:
    """Strict win condition: ``hyprctl -j activewindow`` must name our address.

    Polls briefly because the compositor applies focus asynchronously; a
    strategy whose dispatch answered "ok" without the window taking focus
    (wrong workspace, swallowed event, lying exit status) is NOT a win.  A
    verification that cannot complete inside the budget fails the strategy
    the same as a wrong answer.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            active = state.active_window()
        except Exception:
            active = None
        if isinstance(active, Mapping) and address_matches(active.get("address", ""), wanted):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_VERIFY_POLL_S)


def _derive_qualified(
    runner: Callable[[Sequence[str]], Any], address: str
) -> str | None:
    """Build the plugin's qualified target from the clients list, if possible."""
    wanted = bare_address(address)
    try:
        result = _coerce_result(runner(("hyprctl", "-j", "clients")))
        clients = json.loads(result.stdout)
    except Exception:
        return None
    for client in clients or []:
        if not isinstance(client, Mapping):
            continue
        try:
            if bare_address(client.get("address", "")) != wanted:
                continue
            return qualified_window_target(client)
        except HyprManagementError:
            return None
        except Exception:
            continue
    return None


def _applicable_strategies(
    address: str,
    qualified: str | None,
    runner: Callable[[Sequence[str]], Any],
    dispatch_timeout: float,
) -> tuple[list[tuple[str, Callable[[], tuple[str, ...]]]], list[tuple[str, str]]]:
    """The strategies that may run, plus the ones skipped and why.

    Applicability is decided BEFORE the race starts, in the main thread, so
    the dry-run barrier (which fires first, in focus_window_race) strictly
    precedes any strategy thread.  A strategy with no app-id mapping is kept
    as an immediately-raising entry so the final error says WHY wlrctl sat
    out instead of silently dropping it.
    """
    runners: list[tuple[str, Callable[[], tuple[str, ...]]]] = []
    skipped: list[tuple[str, str]] = []

    if _env_enabled("BRAND_DISABLE_PLUGIN"):
        skipped.append(("plugin", "BRAND_DISABLE_PLUGIN=1"))
    elif qualified is None:
        skipped.append(("plugin", "no qualified window target available"))
    elif not _plugin_loaded(runner):
        skipped.append(("plugin", "brand plugin not listed by hyprctl plugin list"))
    else:
        runners.append(
            (
                "plugin",
                lambda: _run_native(
                    ("hyprctl", "dispatch", "brand:manage", f"focus,{qualified}"),
                    dispatch_timeout,
                    runner,
                ),
            )
        )

    app_id = _app_id_for_address(runner, address)
    if runner is subprocess_runner and shutil.which("wlrctl") is None:
        skipped.append(("wlrctl", "wlrctl not on PATH"))
    elif app_id is not None:
        runners.append(("wlrctl", lambda: _wlrctl_focus(app_id, runner, dispatch_timeout)))
    else:

        def _no_app_id() -> tuple[str, ...]:
            raise FocusRaceError(f"no app-id for {address} in clients list")

        runners.append(("wlrctl", _no_app_id))

    runners.append(
        (
            "legacy",
            lambda: _run_native(
                ("hyprctl", "dispatch", "focuswindow", f"address:{bare_address(address)}"),
                dispatch_timeout,
                runner,
            ),
        )
    )
    return runners, skipped


def _race_strategy(
    name: str,
    thunk: Callable[[], tuple[str, ...]],
    wanted: str,
    state: StateClient,
    verify_timeout: float,
    outcomes: dict[str, tuple[bool, str, tuple[str, ...] | None]],
    lock: threading.Lock,
    won: threading.Event,
) -> None:
    """One strategy's race entry: dispatch, then strict verification.

    Any exception -- dispatch failure, verify timeout, anything unexpected --
    is recorded as this strategy's loss, never as a dead thread: a worker
    must never kill the race.
    """
    ok, err, command = False, "", None
    try:
        command = thunk()
    except Exception as exc:  # noqa: BLE001 -- the race degrades, never dies
        err = f"{name}: {exc}"
    else:
        if won.is_set():
            return  # decided already; don't pile verification queries on
        if _verify_focus(state, wanted, timeout=verify_timeout):
            ok = True
        else:
            err = f"{name}: dispatch claimed success but activewindow never showed {wanted}"
            command = None
    with lock:
        outcomes[name] = (ok, err, command)


def _describe_plans(
    address: str,
    qualified: str | None,
    runner: Callable[[Sequence[str]], Any],
) -> list[str]:
    """Human-readable plan list for BRAND_DRY_RUN=1 (no threads, no dispatch)."""
    runners, skipped = _applicable_strategies(address, qualified, runner, _DISPATCH_TIMEOUT_S)
    plans = []
    thunk_argv = {
        "plugin": f"hyprctl dispatch brand:manage focus,{qualified}",
        "legacy": f"hyprctl dispatch focuswindow address:{bare_address(address)}",
    }
    for name, _thunk in runners:
        if name == "wlrctl":
            app_id = _app_id_for_address(runner, address)
            plans.append(f"wlrctl: wlrctl toplevel focus {app_id!r}" if app_id else "wlrctl: would raise (no app-id)")
        else:
            plans.append(f"{name}: {thunk_argv[name]}")
    for name, reason in skipped:
        plans.append(f"{name}: SKIPPED ({reason})")
    return plans


def focus_window_race(
    address: str,
    *,
    runner: Callable[[Sequence[str]], Any] = subprocess_runner,
    state: StateClient | None = None,
    qualified: str | None = None,
    dispatch_timeout: float = _DISPATCH_TIMEOUT_S,
    verify_timeout: float = _VERIFY_TIMEOUT_S,
    race_timeout: float = _RACE_TIMEOUT_S,
) -> tuple[str, tuple[str, ...]]:
    """Focus the window at ADDRESS, hyper-racing three strategies.

    All applicable strategies fire at once (one daemon thread each); the
    first STRICTLY VERIFIED win returns ``(winner_name, winning_argv)`` where
    winner_name is ``"plugin"``, ``"wlrctl"``, or ``"legacy"``.  Under
    ``BRAND_DRY_RUN=1`` nothing runs and ``("dry-run", ())`` is returned.
    Raises :class:`FocusRaceError` carrying every strategy's error (and every
    skip reason) if all fail.
    """
    target = normalize_address(address)

    runner, state = _default_backing(runner, state)

    # Dry-run barrier: fires before any strategy thread starts.
    if _env_enabled("BRAND_DRY_RUN"):
        for plan in _describe_plans(target, qualified, runner):
            log.warning("[dry-run] focus %s: %s", target, plan)
        return (DRY_RUN_WINNER, ())

    if state is None:
        from hypr_management import HyprctlStateClient

        state = HyprctlStateClient(runner)
    if qualified is None:
        qualified = _derive_qualified(runner, target)

    runners, skipped = _applicable_strategies(target, qualified, runner, dispatch_timeout)

    outcomes: dict[str, tuple[bool, str, tuple[str, ...] | None]] = {}
    lock = threading.Lock()
    won = threading.Event()
    for name, thunk in runners:
        threading.Thread(
            target=_race_strategy,
            args=(name, thunk, target, state, verify_timeout, outcomes, lock, won),
            name=f"brand-focus-{name}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + race_timeout
    while True:
        with lock:
            for name in _STRATEGY_PREFERENCE:
                res = outcomes.get(name)
                if res is not None and res[0]:
                    won.set()
                    assert res[2] is not None
                    return (name, res[2])
            if len(outcomes) >= len(runners):
                break  # everyone reported; no winner among them
        if time.monotonic() >= deadline:
            break  # hung stragglers; daemon threads die with the process
        time.sleep(_RACE_QUANTUM_S)

    errors = [f"{name}: skipped ({reason})" for name, reason in skipped]
    for name, _thunk in runners:
        res = outcomes.get(name)
        if res is None:
            errors.append(f"{name}: timed out without reporting")
        elif not res[0]:
            errors.append(res[1])
    raise FocusRaceError(
        f"focus_window_race {address}: all strategies failed: " + "; ".join(errors)
    )


__all__ = [
    "DRY_RUN_WINNER",
    "FocusRaceError",
    "focus_window_race",
    "load_mock_compositor",
]
