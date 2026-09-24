"""CONNECTION SMOKE, not a model result.

Runs the real worker (python -m jev_ultrafast.worker) over its stdin/stdout protocol with a dedicated Chrome
and the local fixture page. This script plays the Host but runs no model: it answers every text.decide
request with a fixed WAIT and never generates text. It checks that the automation Chrome and its Browser
Harness daemon start, observe the fixture, execute WAIT, stop (discarding a late result), and are gone
after close, while the user's own Chrome processes are untouched. Windows only (process checks).

    python scripts/worker_smoke.py            # JEV_CHROME_PATH optional; uses a temporary profile
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def processes(name):
    """[(pid, command line)] for processes with this image name, read independently of the worker."""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"Get-CimInstance Win32_Process -Filter \"Name='{name}'\" | Select-Object ProcessId,CommandLine"
        " | ConvertTo-Json -Compress"
    )
    output = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=60
    ).stdout.decode("utf-8", "replace").strip()
    rows = json.loads(output) if output else []
    rows = [rows] if isinstance(rows, dict) else rows
    return [(row["ProcessId"], row.get("CommandLine") or "") for row in rows]


def fixed_wait(request):
    """The connection smoke's only 'decision': WAIT with probability 1, whatever the page shows."""
    question = request["spec"]["questions"][0]
    ids = [candidate["id"] for candidate in question["candidates"]]
    assert "WAIT" in ids, ids
    return {
        "type": "text-decide",
        "answers": [
            {
                "questionId": question["id"],
                "kind": "choice",
                "selectedCandidateId": "WAIT",
                "probabilities": [{"candidateId": i, "probability": float(i == "WAIT")} for i in ids],
            }
        ],
        "traceId": "connection-smoke-fixed-wait",
    }


class Worker:
    def __init__(self, env, log):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "jev_ultrafast.worker"],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
        )
        self.messages = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line.decode("utf-8", "strict")))

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def command(self, name, body=None):
        self.next_id += 1
        self.send({"type": "command", "id": self.next_id, "name": name, **({"body": body} if body else {})})
        return self.next_id

    def wait(self, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if predicate(message):
                return message

    def response(self, command_id, timeout=90):
        return self.wait(lambda m: m["type"] == "response" and m["id"] == command_id, timeout)


def main():
    if sys.platform != "win32":
        raise SystemExit("This smoke checks Windows processes; run it on Windows.")
    temp = Path(tempfile.mkdtemp(prefix="jev-smoke-"))
    profile, runtime = temp / "chrome-profile", temp / "runtime"
    env = {**os.environ, "JEV_BROWSER_PROFILE_DIR": str(profile), "JEV_RUNTIME_DIR": str(runtime)}
    user_chrome = {pid for pid, line in processes("chrome.exe") if str(profile) not in line and "--type=" not in line}
    daemons_before = {pid for pid, line in processes("python.exe") if "browser_harness.daemon" in line}
    checks, timings = {}, {}
    log = open(temp / "worker.log", "wb")
    worker = Worker(env, log)
    try:
        started = time.perf_counter()
        response = worker.response(worker.command("preset", {"scenario": "travel"}))
        timings["start_ms"] = round((time.perf_counter() - started) * 1000)
        state = response.get("state") or {}
        page = state.get("page") or {}
        checks["started"] = response["ok"]
        checks["fixture_observed"] = page.get("url", "").startswith("http://127.0.0.1:") and page.get(
            "title"
        ) == "Forma · Find a place to slow down"
        checks["elements_indexed"] = len(state.get("elements", [])) >= 5
        checks["screenshot"] = len(page.get("screenshot", "")) > 1000
        ours = [pid for pid, line in processes("chrome.exe") if str(profile) in line]
        daemons = {pid for pid, line in processes("python.exe") if "browser_harness.daemon" in line} - daemons_before
        checks["dedicated_chrome_running"] = bool(ours)
        checks["own_daemon_running"] = bool(daemons)

        started = time.perf_counter()
        tick = worker.command("tick")
        request = worker.wait(lambda m: m["type"] == "ai_request")
        checks["one_decide_request_for_wait"] = request["kind"] == "decide"
        worker.send({"type": "ai_result", "id": request["id"], "ok": True, "result": fixed_wait(request)})
        state = worker.response(tick)["state"]
        timings["tick_ms"] = round((time.perf_counter() - started) * 1000)
        checks["wait_executed"] = [h["action"] for h in state["history"]] == ["Wait for the page to update"]
        checks["single_call"] = state["history"][0]["decision_calls"] == 1

        predict = worker.command("predict")
        late = worker.wait(lambda m: m["type"] == "ai_request")
        stop = worker.command("stop")
        checks["stop_answered"] = worker.response(stop)["ok"]
        checks["decision_stopped"] = worker.response(predict)["error"]["code"] == "stopped"
        worker.send({"type": "ai_result", "id": late["id"], "ok": True, "result": fixed_wait(late)})
        state = worker.response(worker.command("state"))["state"]
        checks["late_result_discarded"] = state["status"] == "stopped" and state["decision"] is None
        checks["nothing_executed_after_stop"] = len(state["history"]) == 1

        closed = worker.response(worker.command("close"))
        checks["closed"] = closed["ok"] and closed["state"]["status"] == "idle"
        checks["worker_exited"] = worker.process.wait(30) == 0
        deadline = time.monotonic() + 15
        while [pid for pid, line in processes("chrome.exe") if str(profile) in line] and time.monotonic() < deadline:
            time.sleep(0.5)
        checks["dedicated_chrome_gone"] = not [pid for pid, line in processes("chrome.exe") if str(profile) in line]
        running = {pid for pid, _ in processes("python.exe")}
        checks["own_daemon_gone"] = not (daemons & running)
        checks["user_chrome_untouched"] = user_chrome <= {pid for pid, _ in processes("chrome.exe")}
        checks["runtime_removed"] = not any(runtime.iterdir()) if runtime.exists() else True
        checks["profile_kept"] = (profile / "Local State").exists()
    finally:
        if worker.process.poll() is None:
            worker.process.kill()
        log.close()
    result = {
        "kind": "CONNECTION SMOKE - fixed WAIT answers, no model",
        "passed": all(checks.values()),
        "checks": checks,
        "timings": timings,
        "worker_log": (temp / "worker.log").read_text(encoding="utf-8", errors="replace").strip()[-2000:],
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    shutil.rmtree(temp, ignore_errors=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
