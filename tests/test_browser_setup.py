"""Dedicated-browser setup without launching Chrome: command line, profile safety, harness isolation,
process ownership and the loopback fixture server."""

import http.client
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from jev_ultrafast import chrome
from jev_ultrafast.fixtures import FixtureServer

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")


def test_dedicated_chrome_command_line(tmp_path):
    profile = tmp_path / "App Data" / "chrome-profile"
    command = chrome.chrome_command("C:/Chrome/chrome.exe", profile)
    assert command[0] == "C:/Chrome/chrome.exe"
    assert f"--user-data-dir={profile}" in command
    assert "--remote-debugging-port=0" in command
    assert {"--no-first-run", "--no-default-browser-check"} <= set(command)
    assert command[-1] == "about:blank"
    assert not any(a.startswith("--headless") or "enable-automation" in a for a in command)
    assert "--headless=new" in chrome.chrome_command("chrome", profile, headless=True)


def test_chrome_path_comes_from_the_host(tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"")
    assert chrome.find_chrome({"JEV_CHROME_PATH": str(executable)}) == str(executable)
    with pytest.raises(chrome.BrowserSetupError, match="JEV_CHROME_PATH"):
        chrome.find_chrome({"JEV_CHROME_PATH": str(tmp_path / "missing.exe")})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows install locations")
def test_installed_app_environment_still_finds_chrome(tmp_path, monkeypatch):
    # An installed Nimi App inherits no ProgramFiles variables.
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "SystemDrive"):
        monkeypatch.delenv(variable, raising=False)
    registered = tmp_path / "registered" / "chrome.exe"
    registered.parent.mkdir()
    registered.write_bytes(b"")
    monkeypatch.setattr(chrome, "_registered_chrome_paths", lambda: [registered])
    assert chrome.find_chrome({}) == str(registered)

    monkeypatch.setattr(chrome, "_registered_chrome_paths", lambda: [])
    standard = tmp_path / "Program Files" / "Google" / "Chrome" / "Application" / "chrome.exe"
    standard.parent.mkdir(parents=True)
    standard.write_bytes(b"")
    assert chrome.find_chrome({"SystemDrive": str(tmp_path)}) == str(standard)
    with pytest.raises(chrome.BrowserSetupError, match="not found"):
        chrome.find_chrome({})


def test_downloads_stay_inside_the_automation_profile(tmp_path):
    profile = tmp_path / "profile"
    chrome.keep_downloads_in_profile(profile)
    preferences = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert preferences["download"] == {"default_directory": str(profile / "Downloads"), "prompt_for_download": False}
    assert preferences["savefile"]["default_directory"] == str(profile / "Downloads")


def test_personal_browser_profiles_are_refused(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path / "Local"), "XDG_CONFIG_HOME": str(tmp_path / "config")}
    personal = chrome.user_profile_roots(env)[0]
    for candidate in (personal, personal / "Default"):
        with pytest.raises(chrome.BrowserSetupError, match="personal browser profile"):
            chrome.check_profile_dir(candidate, env)
    owned = tmp_path / "Nimi" / "jev" / "chrome-profile"
    assert chrome.check_profile_dir(owned, env) == owned.resolve()


def test_devtools_port_is_read_only_when_completely_written(tmp_path):
    active = tmp_path / "DevToolsActivePort"
    assert chrome.read_devtools_active_port(tmp_path) is None
    active.write_text("51234\n", encoding="utf-8")
    assert chrome.read_devtools_active_port(tmp_path) is None
    active.write_text("51234\n/devtools/browser/0f1e-2d3c\n", encoding="utf-8")
    assert chrome.read_devtools_active_port(tmp_path) == (51234, "/devtools/browser/0f1e-2d3c")
    active.write_text("99999\n/devtools/browser/x\n", encoding="utf-8")
    assert chrome.read_devtools_active_port(tmp_path) is None


def test_profile_directory_comes_from_the_host_or_is_temporary(tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"")
    temporary = chrome.DedicatedBrowser.from_env({"JEV_CHROME_PATH": str(executable)})
    assert temporary.temporary_profile and temporary.profile_dir.is_dir()
    temporary.close()
    assert not temporary.profile_dir.exists()
    owned = chrome.DedicatedBrowser.from_env(
        {"JEV_CHROME_PATH": str(executable), "JEV_BROWSER_PROFILE_DIR": str(tmp_path / "p"), "JEV_CHROME_HEADLESS": "1"}
    )
    assert not owned.temporary_profile and owned.profile_dir == tmp_path / "p" and owned.headless


def test_a_browser_that_exits_during_startup_fails_closed(tmp_path):
    # The Python interpreter rejects Chrome's flags and exits at once, like a Chrome handing off to another instance.
    browser = chrome.DedicatedBrowser(sys.executable, tmp_path / "profile")
    with pytest.raises(chrome.BrowserSetupError, match="exited during startup"):
        browser.start()
    browser.close()


@windows
def test_profile_held_by_a_running_browser_is_detected(tmp_path):
    lock = tmp_path / "lockfile"
    with open(lock, "w"):
        assert chrome.profile_in_use(tmp_path)
    assert not chrome.profile_in_use(tmp_path)  # A stale lock is removed.
    assert not lock.exists()


def _alive(pid):
    """Existence check without os.kill, which terminates processes on Windows."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@windows
def test_close_ends_the_whole_launched_tree_and_nothing_else(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open({str(marker)!r}, "w").write(str(child.pid))
        time.sleep(120)
        """
    )
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    job = chrome.ProcessJob()
    launched = job.spawn([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 30
        while not (marker.exists() and marker.read_text().strip()):
            assert time.monotonic() < deadline, "the stand-in never started its child"
            time.sleep(0.05)
        grandchild = int(marker.read_text())
        # Suspended start: the stand-in and its child are both in the job (a venv python.exe is a
        # redirector that starts the base interpreter, so each counts as two processes).
        assert job.active_processes() >= 2
        job.close()
        deadline = time.monotonic() + 10
        while _alive(grandchild) or _alive(launched.pid):
            assert time.monotonic() < deadline, "the launched tree survived close()"
            time.sleep(0.05)
        assert _alive(bystander.pid)  # Only what this launch created was terminated.
    finally:
        bystander.kill()
        job.close()


def _run_python(code, env, *args):
    process = subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True, timeout=60)
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    return json.loads(process.stdout)


def test_harness_is_bound_to_worker_paths_before_import(tmp_path):
    code = textwrap.dedent(
        """
        import json, os, sys
        from jev_ultrafast import harness
        try:
            harness.cdp("Target.getTargets")
        except harness.HarnessError as error:
            refused = str(error)
        name = harness.configure(sys.argv[1], sys.argv[2])
        keys = ("BU_NAME", "BU_CDP_WS", "BU_CDP_URL", "BU_BROWSER_ID", "BH_HOME", "BH_RUNTIME_DIR",
                "BH_AGENT_WORKSPACE", "NO_PROXY", "BH_TAB_MARKER")
        imported = any(m.startswith("browser_harness") for m in sys.modules)
        print(json.dumps({"refused": refused, "name": name, "env": {k: os.environ.get(k) for k in keys},
                          "imported": imported}))
        """
    )
    env = {
        **os.environ,
        "BU_CDP_WS": "ws://203.0.113.9:9222/devtools/browser/someone-elses",
        "BU_CDP_URL": "http://127.0.0.1:9222",
        "BU_NAME": "default",
        "BU_BROWSER_ID": "cloud-browser",
        "BH_AGENT_WORKSPACE": str(tmp_path / "user-workspace"),
        "NO_PROXY": "example.com",
    }
    data = _run_python(code, env, str(tmp_path / "home"), str(tmp_path / "endpoints"))
    assert "No dedicated automation browser" in data["refused"]
    assert data["name"].startswith("jev-") and data["env"]["BU_NAME"] == data["name"]
    for inherited in ("BU_CDP_WS", "BU_CDP_URL", "BU_BROWSER_ID", "BH_AGENT_WORKSPACE"):
        assert data["env"][inherited] is None
    assert data["env"]["BH_HOME"] == str((tmp_path / "home").resolve())
    assert data["env"]["BH_RUNTIME_DIR"] == str((tmp_path / "endpoints").resolve())
    assert set(data["env"]["NO_PROXY"].split(",")) >= {"example.com", "127.0.0.1", "localhost"}
    assert data["env"]["BH_TAB_MARKER"] == "0" and data["imported"] is False


def test_harness_refuses_to_bind_after_an_early_import(tmp_path):
    code = textwrap.dedent(
        """
        import json, sys
        import browser_harness.helpers
        from jev_ultrafast import harness
        try:
            harness.configure(sys.argv[1])
            print(json.dumps("bound"))
        except harness.HarnessError as error:
            print(json.dumps(str(error)))
        """
    )
    assert "imported before" in _run_python(code, dict(os.environ), str(tmp_path / "home"))


def _request(server, method="GET", path="/fixture.html?scenario=travel", host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host or f"127.0.0.1:{server.port}")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


def test_fixture_server_is_loopback_only_and_serves_only_the_fixture():
    server = FixtureServer()
    try:
        assert server.httpd.server_address[0] == "127.0.0.1"
        assert server.url("research") == f"http://127.0.0.1:{server.port}/fixture.html?scenario=research"
        status, body, headers = _request(server)
        assert status == 200 and b"forma." in body
        assert headers["Content-Type"] == "text/html; charset=utf-8" and headers["Cache-Control"] == "no-store"
        for path in ("/", "/index.html", "/app.js", "/api/state", "/../pyproject.toml", "/static/fixture.html"):
            assert _request(server, path=path)[0] == 404, path
        assert _request(server, host=f"localhost:{server.port}")[0] == 403
        assert _request(server, host="attacker.test")[0] == 403
        assert _request(server, method="POST")[0] == 501
        assert _request(server, method="HEAD")[:2] == (200, b"")
    finally:
        server.close()
