"""Browser Harness bound to the worker's own automation Chrome.

Browser Harness reads BU_NAME and BH_* and loads workspace .env files when it is imported, and its daemon
prefers BU_CDP_WS over BU_CDP_URL. ``configure()`` therefore runs before the first import: it removes
inherited connection overrides, points every harness path at worker-owned directories and picks a unique
daemon name. Until then ``cdp()`` refuses to run, so nothing can fall back to the user's own Chrome.
"""

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

INHERITED = (
    "BU_CDP_WS",
    "BU_CDP_URL",
    "BU_BROWSER_ID",
    "BROWSER_USE_API_KEY",
    "BH_RUNTIME_DIR",
    "BH_TMP_DIR",
    "BH_AGENT_WORKSPACE",
    "BH_CONFIG_DIR",
    "BH_RUNTIME_DIR_SHARED",
    "BH_TMP_DIR_SHARED",
    "BROWSER_HARNESS_HOME",
)
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
DAEMON_STARTUP_TIMEOUT = 30
_bound = None  # (home, runtime, name)


class HarnessError(RuntimeError):
    """Browser Harness is not bound to this worker's dedicated browser."""


def _imported():
    return any(m == "browser_harness" or m.startswith("browser_harness.") for m in sys.modules)


def configure(home, runtime=None, name=None):
    """Bind Browser Harness to worker-owned paths and a unique daemon name. Returns the name."""
    global _bound
    home = Path(home).resolve()
    runtime = Path(runtime).resolve() if runtime else home / "runtime"
    name = name or f"jev-{os.getpid()}-{secrets.token_hex(4)}"
    if not NAME.fullmatch(name) or name == "default":
        raise ValueError("The daemon name must be unique and match [A-Za-z0-9_-]{1,64}")
    if _bound:
        if _bound[:2] == (home, runtime):
            return _bound[2]
        raise HarnessError("Browser Harness is already bound to other directories in this process")
    if _imported():
        raise HarnessError("Browser Harness was imported before the dedicated browser was configured")
    for key in INHERITED:
        os.environ.pop(key, None)
    # The daemon's urllib discovery and websocket honour proxy settings; loopback DevTools must bypass them.
    for key in ("NO_PROXY", "no_proxy"):
        hosts = [h.strip() for h in os.environ.get(key, "").split(",") if h.strip()]
        os.environ[key] = ",".join(hosts + [h for h in ("127.0.0.1", "localhost") if h not in hosts])
    home.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    # BH_RUNTIME_DIR isolates this daemon's endpoint files; BH_HOME holds its log, config and workspace.
    os.environ.update(
        BH_HOME=str(home),
        BH_RUNTIME_DIR=str(runtime),
        BU_NAME=name,
        BH_TAB_MARKER="0",  # Never rewrite page titles; the title is part of the observed state.
        BH_UPDATE_CHECK="0",
        BH_TELEMETRY="0",
    )
    _bound = (home, runtime, name)
    return name


def bound_name():
    if not _bound:
        raise HarnessError(
            "No dedicated automation browser is configured; refusing the default Browser Harness connection."
        )
    return _bound[2]


def cdp(method, session_id=None, **params):
    bound_name()
    from browser_harness.helpers import cdp as harness_cdp

    return harness_cdp(method, session_id=session_id, **params)


def daemon_command():
    """The command Browser Harness itself uses to start a daemon (admin.ensure_daemon)."""
    return [sys.executable, "-m", "browser_harness.daemon"]


def _log_tail(path):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1] if lines else ""


def start_daemon(cdp_url, spawn):
    """Start this worker's daemon on the dedicated Chrome's DevTools endpoint and return its process.

    ``spawn`` is the browser session's process job, so the daemon ends with that Chrome, or with the
    worker. BU_CDP_URL is resolved by the daemon's get_ws_url(); BU_CDP_WS was removed by configure().
    """
    name = bound_name()
    # Also set it process-wide: any later harness call can only resolve this endpoint, never local discovery.
    os.environ["BU_CDP_URL"] = cdp_url
    from browser_harness import _ipc, admin

    if admin.daemon_alive(name):
        admin.restart_daemon(name)  # This worker's previous daemon, whose Chrome is gone.
    options = {"env": {**os.environ, "BU_NAME": name, "BU_CDP_URL": cdp_url}}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    log = _ipc.log_path(name)
    with open(log, "ab") as stderr:
        process = spawn(daemon_command(), stderr=stderr, **options)
    deadline = time.monotonic() + DAEMON_STARTUP_TIMEOUT
    while not admin.daemon_alive(name):
        if process.poll() is not None or time.monotonic() > deadline:
            tail = _log_tail(log)
            raise HarnessError("Browser Harness daemon did not start" + (f": {tail}" if tail else ""))
        time.sleep(0.05)
    if admin.daemon_browser_kind(name) != "cdp":
        stop_daemon()
        raise HarnessError("Browser Harness did not attach through the dedicated DevTools endpoint")
    return process


def stop_daemon():
    """Stop only this worker's daemon. Best effort; the Chrome job object is the backstop."""
    if not _bound:
        return
    try:
        if _imported():
            from browser_harness import admin

            admin.restart_daemon(_bound[2])
    except Exception as error:  # Shutdown continues; the Chrome job object is the backstop.
        print(f"jev: daemon shutdown: {error}", file=sys.stderr, flush=True)
    finally:
        os.environ.pop("BU_CDP_URL", None)
