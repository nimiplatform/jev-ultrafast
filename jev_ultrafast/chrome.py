"""The worker's dedicated automation Chrome: its own profile, an ephemeral DevTools port and a process tree
that only the worker owns. The user's own Chrome and profiles are never attached to or touched."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import harness

STARTUP_TIMEOUT = 30
CLOSE_TIMEOUT = 5
# Relative to %LOCALAPPDATA%, ~/Library/Application Support or ~/.config: personal profiles, never automated.
USER_PROFILES = {
    "win32": (
        "Google/Chrome/User Data",
        "Google/Chrome SxS/User Data",
        "Google/Chrome Beta/User Data",
        "Google/Chrome Dev/User Data",
        "Chromium/User Data",
        "Microsoft/Edge/User Data",
        "BraveSoftware/Brave-Browser/User Data",
    ),
    "darwin": (
        "Google/Chrome",
        "Google/Chrome Canary",
        "Google/Chrome Beta",
        "Chromium",
        "Microsoft Edge",
        "BraveSoftware/Brave-Browser",
    ),
    "linux": ("google-chrome", "google-chrome-beta", "chromium", "microsoft-edge", "BraveSoftware/Brave-Browser"),
}


class BrowserSetupError(RuntimeError):
    """The dedicated automation browser could not be started safely."""


def _registered_chrome_paths():
    """chrome.exe as registered under App Paths by Chrome's installer (per user, then per machine)."""
    try:
        import winreg
    except ImportError:
        return []
    paths = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe") as key:
                value, kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str) and value.strip():
            paths.append(Path(os.path.expandvars(value.strip().strip('"'))))
    return paths


def find_chrome(env=None):
    """JEV_CHROME_PATH from the Host, else a standard Chrome install location."""
    env = os.environ if env is None else env
    explicit = env.get("JEV_CHROME_PATH")
    if explicit:
        if not Path(explicit).is_file():
            raise BrowserSetupError(f"JEV_CHROME_PATH is not a file: {explicit}")
        return str(Path(explicit))
    candidates = []
    if sys.platform == "win32":
        # An installed App gets only a small inherited environment (no ProgramFiles), so the path
        # Chrome's installer registers comes first, then the standard install folders.
        candidates.extend(_registered_chrome_paths())
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = env.get(variable) or os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
        drive = env.get("SystemDrive") or os.environ.get("SystemDrive")
        if drive:
            for folder in ("Program Files", "Program Files (x86)"):
                candidates.append(Path(drive + "\\") / folder / "Google/Chrome/Application/chrome.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            if found := shutil.which(name):
                candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise BrowserSetupError("Google Chrome was not found. Set JEV_CHROME_PATH to chrome.exe.")


def user_profile_roots(env=None):
    env = os.environ if env is None else env
    home = Path.home()
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData/Local")
        names = USER_PROFILES["win32"]
    elif sys.platform == "darwin":
        base, names = home / "Library/Application Support", USER_PROFILES["darwin"]
    else:
        base, names = Path(env.get("XDG_CONFIG_HOME") or home / ".config"), USER_PROFILES["linux"]
    return [base / name for name in names]


def check_profile_dir(profile_dir, env=None):
    """Refuse a personal browser profile; automation always runs in an App-owned user-data-dir."""
    resolved = Path(profile_dir).resolve()
    for root in user_profile_roots(env):
        root = root.resolve()
        if resolved == root or root in resolved.parents:
            raise BrowserSetupError(f"Refusing to automate a personal browser profile: {resolved}")
    return resolved


def keep_downloads_in_profile(profile_dir):
    """Point downloads into the automation profile, which is removed with the run, never the user's folder."""
    preferences = Path(profile_dir) / "Default" / "Preferences"
    try:
        current = json.loads(preferences.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    folder = str(Path(profile_dir) / "Downloads")
    current.setdefault("download", {}).update(default_directory=folder, prompt_for_download=False)
    current.setdefault("savefile", {}).update(default_directory=folder)
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(json.dumps(current), encoding="utf-8")


def chrome_command(chrome, profile_dir, *, headless=False):
    """Command line for the dedicated automation Chrome. Port 0 lets Chrome pick a free loopback port."""
    command = [
        str(chrome),
        f"--user-data-dir={Path(profile_dir)}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--hide-crash-restore-bubble",
        "--window-size=1280,900",
    ]
    if headless:
        command.append("--headless=new")
    command.append("about:blank")
    return command


def read_devtools_active_port(profile_dir):
    """(port, browser websocket path) once Chrome has fully written DevToolsActivePort, else None."""
    try:
        lines = (Path(profile_dir) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if not 0 < port < 65536 or not path.startswith("/devtools/browser/"):
        return None
    return port, path


def profile_in_use(profile_dir):
    """True when a running Chrome holds this user-data-dir."""
    profile_dir = Path(profile_dir)
    if sys.platform == "win32":
        lock = profile_dir / "lockfile"
        try:
            lock.unlink()  # A stale lock from a crashed Chrome can be removed; a live one is held open.
        except FileNotFoundError:
            return False
        except PermissionError:
            return True
        return False
    try:
        pid = int(os.readlink(profile_dir / "SingletonLock").rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # POSIX existence probe; this branch never runs on Windows.
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_TERMINATE, _PROCESS_SET_QUOTA, _PROCESS_SUSPEND_RESUME = 0x0001, 0x0100, 0x0800
    CREATE_SUSPENDED, CREATE_NEW_PROCESS_GROUP = 0x00000004, 0x00000200

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ("r_ops", "w_ops", "o_ops", "r_bytes", "w_bytes", "o_bytes")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _Accounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _check(ok):
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return ok

    def _open(pid, access):
        return _check(_kernel32.OpenProcess(access, False, pid))

    class _Job:
        """A kill-on-close job: closing it, or the worker dying, terminates every process inside."""

        def __init__(self):
            self.handle = _check(_kernel32.CreateJobObjectW(None, None))
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            _check(_kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))

        def assign(self, pid):
            process = _open(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE)
            try:
                _check(_kernel32.AssignProcessToJobObject(self.handle, process))
            finally:
                _kernel32.CloseHandle(process)

        def active_processes(self):
            accounting = _Accounting()
            _check(
                _kernel32.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                )
            )
            return accounting.ActiveProcesses

        def terminate(self):
            _kernel32.TerminateJobObject(self.handle, 1)

        def close(self):
            if self.handle:
                _kernel32.CloseHandle(self.handle)
                self.handle = None

    def _resume(pid):
        process = _open(pid, _PROCESS_SUSPEND_RESUME)
        try:
            if _ntdll.NtResumeProcess(process) != 0:
                raise OSError("NtResumeProcess failed")
        finally:
            _kernel32.CloseHandle(process)


class ProcessJob:
    """Every process the worker starts for one browser session, terminated together.

    On Windows each process starts suspended, joins one kill-on-close job object and only then runs, so it
    and everything it starts (Chrome's helpers, the interpreter behind a venv launcher) end with close() or
    with the worker if the worker dies. Nothing outside the job is ever signalled. Elsewhere each process
    gets its own session and process group.
    """

    def __init__(self):
        self.processes = []
        self._job = _Job() if sys.platform == "win32" else None

    def spawn(self, command, **options):
        # Never inherit the worker's protocol pipes.
        for stream in ("stdin", "stdout", "stderr"):
            options.setdefault(stream, subprocess.DEVNULL)
        try:
            if self._job is None:
                process = subprocess.Popen(command, start_new_session=True, **options)
            else:
                flags = options.pop("creationflags", 0) | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
                process = subprocess.Popen(command, creationflags=flags, **options)
        except OSError as error:
            raise BrowserSetupError(f"Could not start {Path(command[0]).name}: {error}") from None
        if self._job is not None:
            try:
                self._job.assign(process.pid)
                _resume(process.pid)
            except OSError as error:
                process.kill()
                process.wait(CLOSE_TIMEOUT)
                raise BrowserSetupError(f"Could not place a process under the worker's job: {error}") from None
        self.processes.append(process)
        return process

    def active_processes(self):
        if self._job is not None:
            return self._job.active_processes()
        return sum(process.poll() is None for process in self.processes)

    def close(self):
        """Terminate every process this job started, and their descendants. Idempotent."""
        if sys.platform == "win32":
            if self._job is not None:
                self._job.terminate()
                self._job.close()
                self._job = None
        else:
            for process in self.processes:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    if process.poll() is not None:
                        break
                    try:
                        os.killpg(process.pid, sig)
                    except OSError:
                        break
                    try:
                        process.wait(3)
                    except subprocess.TimeoutExpired:
                        continue
        for process in self.processes:
            try:
                process.wait(CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass


def _loopback_json(url, timeout):
    # Never route loopback DevTools discovery through a system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _close_browser(ws_url, timeout=2.0):
    """Ask Chrome to close normally so the automation profile is saved. Best effort."""
    try:
        from websockets.sync.client import connect

        with connect(ws_url, open_timeout=timeout, close_timeout=0.5, proxy=None, max_size=2**20) as ws:
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.recv(timeout=timeout)
    except Exception:  # Chrome may drop the connection while closing; the process wait decides.
        pass


class DedicatedBrowser:
    """The worker's automation Chrome and its Browser Harness daemon. close() touches only what start() made."""

    def __init__(self, chrome_path, profile_dir, *, headless=False, temporary_profile=False, env=None):
        self.chrome_path = str(chrome_path)
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.temporary_profile = temporary_profile
        self.env = os.environ if env is None else env
        self.job = None
        self.chrome = None
        self.daemon = None
        self.port = None
        self.ws_url = None

    @classmethod
    def from_env(cls, env=None):
        """JEV_CHROME_PATH and JEV_BROWSER_PROFILE_DIR come from the Host; without a profile directory,
        a temporary one is created and removed on close."""
        env = os.environ if env is None else env
        chrome = find_chrome(env)
        profile = env.get("JEV_BROWSER_PROFILE_DIR")
        profile_dir = Path(profile) if profile else Path(tempfile.mkdtemp(prefix="jev-profile-"))
        headless = env.get("JEV_CHROME_HEADLESS") == "1"
        return cls(chrome, profile_dir, headless=headless, temporary_profile=not profile, env=env)

    def start(self):
        check_profile_dir(self.profile_dir, self.env)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if profile_in_use(self.profile_dir):
            raise BrowserSetupError(f"Another Chrome is using the automation profile {self.profile_dir}")
        (self.profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
        keep_downloads_in_profile(self.profile_dir)
        self.job = ProcessJob()
        self.chrome = self.job.spawn(chrome_command(self.chrome_path, self.profile_dir, headless=self.headless))
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not (found := read_devtools_active_port(self.profile_dir)):
            if self.chrome.poll() is not None:
                raise BrowserSetupError(
                    f"Chrome exited during startup (code {self.chrome.returncode}); "
                    f"is {self.profile_dir} open in another Chrome?"
                )
            if time.monotonic() > deadline:
                raise BrowserSetupError("Chrome did not publish its DevTools port in time")
            time.sleep(0.05)
        port, path = found
        while True:
            try:
                version = _loopback_json(f"http://127.0.0.1:{port}/json/version", timeout=2)
                break
            except (OSError, ValueError):
                if time.monotonic() > deadline or self.chrome.poll() is not None:
                    raise BrowserSetupError("The dedicated Chrome's DevTools endpoint did not answer") from None
                time.sleep(0.1)
        # The endpoint must be the browser this launch wrote into its own profile, not another listener.
        if urlsplit(version.get("webSocketDebuggerUrl", "")).path != path:
            raise BrowserSetupError("The DevTools endpoint does not belong to the launched Chrome")
        self.port, self.ws_url = port, version["webSocketDebuggerUrl"]
        self.daemon = harness.start_daemon(f"http://127.0.0.1:{port}", self.job.spawn)
        return self

    def healthy(self):
        if not (self.chrome and self.chrome.poll() is None):
            return False
        try:
            harness.cdp("Target.getTargets")
            return True
        except Exception:
            return False

    def close(self):
        """Stop the daemon, close Chrome normally if possible, then end every process this session started."""
        harness.stop_daemon()
        job, chrome, ws_url = self.job, self.chrome, self.ws_url
        self.job = self.chrome = self.daemon = self.ws_url = None
        if chrome is not None and ws_url and chrome.poll() is None:
            _close_browser(ws_url)
            try:
                chrome.wait(CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        if job is not None:
            job.close()
        if self.temporary_profile:
            for _ in range(20):  # Chrome can release profile files shortly after exit.
                shutil.rmtree(self.profile_dir, ignore_errors=True)
                if not self.profile_dir.exists():
                    break
                time.sleep(0.1)
