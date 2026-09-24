"""The worker protocol over real pipes, with the test playing the Host. No browser, no model, no network."""

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

from jev_ultrafast import worker as w
from jev_ultrafast.agent import RunToken, Stopped
from jev_ultrafast.browser import fingerprint

GOAL = "找到 Casa Flora 并打开 🏡 — «Zürich»"


def page(url="https://example.test/"):
    state = {
        "url": url,
        "title": "Search",
        "text": "Search · 搜索",
        "w": 1120,
        "h": 780,
        "scroll": {"y": 0, "height": 780},
        "screenshot": "",
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def decide_result(request, selected):
    question = request["spec"]["questions"][0]
    ids = [c["id"] for c in question["candidates"]]
    return {
        "type": "text-decide",
        "answers": [
            {
                "questionId": question["id"],
                "kind": "choice",
                "selectedCandidateId": selected,
                "probabilities": [{"candidateId": i, "probability": float(i == selected)} for i in ids],
            }
        ],
        "traceId": "trace",
    }


class FakeBrowser:
    def __init__(self, url, text=""):
        self.url, self.text = url, text
        self.observations, self.acts, self.closed = 0, [], False

    def observe(self, screenshot=True):
        self.observations += 1
        return page(self.url)

    def fresh(self, _page, _action=None):
        return True

    def act(self, action, _page, text=None):
        self.acts.append((action["id"], text))

    def evaluate(self, _expression):
        return self.text

    def close(self):
        self.closed = True


class FakeDedicated:
    def __init__(self):
        self.started = self.closed = False

    def start(self):
        self.started = True
        return self

    def healthy(self):
        return self.started and not self.closed

    def close(self):
        self.closed = True


class FakeFixtures:
    closed = False

    def url(self, scenario):
        return f"http://127.0.0.1:9/fixture.html?scenario={scenario}"

    def close(self):
        self.closed = True


class Host:
    """Runs a Worker over OS pipes. The test sends Host messages and reads worker lines strictly."""

    def __init__(self, browser_text=""):
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        self.to_worker = os.fdopen(in_w, "wb", buffering=0)
        self.from_worker = os.fdopen(out_r, "rb")
        worker_in, worker_out = os.fdopen(in_r, "rb"), os.fdopen(out_w, "wb")
        channel = w.Channel(worker_out)
        ai = w.HostAI(channel)
        self.browsers = []
        self.dedicated, self.fixtures = FakeDedicated(), FakeFixtures()

        def browser(url):
            self.browsers.append(FakeBrowser(url, browser_text))
            return self.browsers[-1]

        self.session = w.Session(
            ai, env={}, open_browser=lambda: self.dedicated, open_fixtures=lambda: self.fixtures,
            browser_factory=browser,
        )
        self.worker = w.Worker(channel, ai, self.session)
        self.messages, self.raw, self.seen, self.stashed = queue.Queue(), [], [], []

        def serve():
            try:
                self.worker.serve(worker_in)
            finally:
                self.worker.shutdown()
                worker_out.close()

        def read():
            for line in self.from_worker:
                self.raw.append(line)
                self.messages.put(json.loads(line.decode("utf-8", "strict")))

        self.server = threading.Thread(target=serve, daemon=True)
        self.server.start()
        threading.Thread(target=read, daemon=True).start()
        self.next_id = 0

    def send(self, message):
        self.to_worker.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")

    def command(self, name, body=None):
        self.next_id += 1
        self.send({"type": "command", "id": self.next_id, "name": name, **({"body": body} if body else {})})
        return self.next_id

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            self.seen.append(message)
            if predicate(message):
                return message
            if message["type"] == "response":
                self.stashed.append(message)

    def response(self, command_id):
        # Responses may arrive in either order (a stopped tick can answer before the stop itself);
        # one drained while waiting for another is still this command's response.
        for index, message in enumerate(self.stashed):
            if message["id"] == command_id:
                return self.stashed.pop(index)
        return self.wait(lambda m: m["type"] == "response" and m["id"] == command_id)

    def ai_request(self):
        return self.wait(lambda m: m["type"] == "ai_request")

    def answer(self, request, selected):
        self.send({"type": "ai_result", "id": request["id"], "ok": True, "result": decide_result(request, selected)})

    def quiet(self, seconds=0.3):
        """Everything the worker sent within `seconds`, without waiting for anything specific."""
        time.sleep(seconds)
        drained = []
        while not self.messages.empty():
            drained.append(self.messages.get())
        self.seen.extend(drained)
        return drained

    def start(self, goal=GOAL):
        response = self.response(self.command("start", {"url": "https://example.test/", "goal": goal}))
        assert response["ok"], response
        return response

    def close(self):
        self.to_worker.close()
        self.server.join(10)


@pytest.fixture
def host():
    h = Host()
    yield h
    h.close()


def test_non_ascii_goal_round_trips_as_strict_utf8(host):
    response = host.start()
    assert response["state"]["goal"] == GOAL
    raw = next(line for line in host.raw if line.startswith(b'{"type":"response","id":1,'))
    assert GOAL.encode("utf-8") in raw and "搜索".encode("utf-8") in raw  # Raw UTF-8, not \u escapes.
    raw.decode("utf-8", "strict")


def test_worker_process_uses_strict_utf8_regardless_of_locale(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "cp936", "PYTHONUTF8": "0", "JEV_RUNTIME_DIR": str(tmp_path)}
    messages = [
        {"type": "command", "id": 1, "name": "start",
         "body": {"url": "https://example.test/", "goal": "🚀 出发", "目标": 1}},
        {"type": "command", "id": 2, "name": "state"},
        {"type": "command", "id": 3, "name": "close"},
    ]
    stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
    process = subprocess.run(
        [sys.executable, "-m", "jev_ultrafast.worker"], input=stdin, capture_output=True, env=env, timeout=60
    )
    assert process.returncode == 0, process.stderr.decode("utf-8", "replace")
    lines = process.stdout.splitlines()
    replies = [json.loads(line.decode("utf-8", "strict")) for line in lines]
    assert replies[0]["error"]["code"] == "invalid_command" and "目标" in replies[0]["error"]["message"]
    assert "目标".encode("utf-8") in lines[0]
    assert replies[1]["ok"] and "Google Flights · real web".encode("utf-8") in lines[1]
    assert replies[2]["ok"] and replies[2]["state"]["status"] == "idle"
    assert list(tmp_path.iterdir()) == []  # The worker removed its runtime directory.


def test_operation_then_target_over_the_protocol(host):
    host.start()
    predict = host.command("predict")
    operation = host.ai_request()
    assert operation["kind"] == "decide" and operation["spec"]["questions"][0]["id"] == "operation"
    assert 59_000 <= operation["timeoutMs"] <= 60_000  # One decision budget for both questions.
    host.answer(operation, "CLICK")
    target = host.ai_request()
    assert target["spec"]["questions"][0]["id"] == "click_target"
    assert target["spec"]["state"] == operation["spec"]["state"] and target["timeoutMs"] <= operation["timeoutMs"]
    host.answer(target, "2")
    decision = host.response(predict)["state"]["decision"]
    assert decision["choice"] == "e3" and decision["calls"] == 2


def test_wait_needs_a_single_call_over_the_protocol(host):
    host.start()
    predict = host.command("predict")
    host.answer(host.ai_request(), "WAIT")
    assert host.response(predict)["state"]["decision"]["calls"] == 1
    assert not any(m["type"] == "ai_request" for m in host.quiet())


def test_stop_discards_a_late_ai_result_without_executing(host):
    host.start()
    tick = host.command("tick")
    request = host.ai_request()
    stop = host.command("stop")
    assert host.response(stop)["ok"]
    failed = host.response(tick)
    assert failed["ok"] is False and failed["error"]["code"] == "stopped"
    host.answer(request, "CLICK")  # Late: must be discarded, never executed.
    assert not any(m["type"] in ("ai_request", "response") for m in host.quiet())
    state = host.response(host.command("state"))["state"]
    assert state["status"] == "stopped" and state["decision"] is None and state["busy"] is None
    browser = host.browsers[0]
    assert browser.acts == []
    # An explicit continue observes the page again and decides afresh.
    observed = browser.observations
    tick = host.command("tick")
    host.answer(host.ai_request(), "WAIT")
    assert host.response(tick)["ok"]
    assert browser.observations > observed and browser.acts == [("wait", None)]


def test_stop_during_text_generation_discards_the_late_text(host):
    host.start()
    predict = host.command("predict")
    host.answer(host.ai_request(), "TYPE_TEXT")  # One TYPE_TEXT candidate: no target question.
    fingerprint_ = host.response(predict)["state"]["page"]["fingerprint"]
    act = host.command("act", {"fingerprint": fingerprint_})
    request = host.ai_request()
    assert request["kind"] == "generate_text" and request["spec"]["type"] == "text-generate"
    host.response(host.command("stop"))
    assert host.response(act)["error"]["code"] == "stopped"
    late = {"type": "text-generate", "text": json.dumps({"text": "book"})}
    host.send({"type": "ai_result", "id": request["id"], "ok": True, "result": late})
    host.quiet()
    assert host.browsers[0].acts == []


def test_commands_run_one_at_a_time_and_state_answers_while_busy(host):
    host.start()
    predict = host.command("predict")
    request = host.ai_request()
    busy = host.response(host.command("predict"))
    assert busy["ok"] is False and busy["error"]["code"] == "busy"
    state = host.response(host.command("state"))
    assert state["ok"] and state["state"]["busy"] == "predict"
    host.answer(request, "WAIT")
    assert host.response(predict)["ok"]


def test_host_errors_and_malformed_results_fail_closed(host):
    host.start()
    predict = host.command("predict")
    request = host.ai_request()
    error = {"code": "AI_NO_ROUTE", "message": "No model"}
    host.send({"type": "ai_result", "id": request["id"], "ok": False, "error": error})
    failed = host.response(predict)
    assert failed["error"]["code"] == "ai_error" and "AI_NO_ROUTE" in failed["error"]["message"]
    predict = host.command("predict")
    request = host.ai_request()
    host.send({"type": "ai_result", "id": request["id"], "ok": True, "result": "CLICK"})
    assert host.response(predict)["error"]["code"] == "ai_error"
    predict = host.command("predict")
    request = host.ai_request()
    invented = decide_result(request, "WAIT")
    invented["answers"][0]["probabilities"][0]["probability"] = 0.5  # No longer sums to 1.
    host.send({"type": "ai_result", "id": request["id"], "ok": True, "result": invented})
    assert host.response(predict)["error"]["code"] == "decision_contract"
    assert host.browsers[0].acts == []


def test_protocol_rejects_bad_bytes_unknown_types_and_fields(host):
    host.to_worker.write(b"\xff\xfe not utf-8\n")
    host.to_worker.write(b'{"type":"command","id":1,"name":"state","extra":true}\n')
    assert host.response(1)["error"]["code"] == "invalid_command"
    for command_id, name, body in [
        (2, "launch", None),
        (3, "predict", {"x": 1}),
        (4, "start", {"url": "javascript:alert(1)", "goal": "x"}),
        (5, "start", {"url": "file:///C:/Windows/win.ini", "goal": "x"}),
        (6, "start", {"url": "https://user:secret@example.test/", "goal": "x"}),
        (7, "start", {"url": "https://example.test/", "goal": " "}),
        (8, "preset", {"scenario": "banking"}),
        (9, "act", {"fingerprint": "not-a-fingerprint"}),
        (10, "auto", {"pace": "yes"}),
    ]:
        host.send({"type": "command", "id": command_id, "name": name, **({"body": body} if body else {})})
        assert host.response(command_id)["error"]["code"] == "invalid_command"
    host.send({"type": "bogus", "id": 11})
    host.send({"type": "command", "id": True, "name": "state"})
    host.to_worker.write(b'{"type":"command","id":12,"name":"state","id":13}\n')
    host.to_worker.write(b'{"type":"command","id":14,"name":"act","body":{"fingerprint":NaN}}\n')
    # A lone surrogate escape cannot become strict UTF-8 text.
    host.to_worker.write(b'{"type":"command","id":15,"name":"start",'
                         b'"body":{"url":"https://example.test/","goal":"\\ud800"}}\n')
    host.to_worker.write(b'{"type":"command","id":16,"name":"state"' + b" " * (w.MAX_MESSAGE_BYTES + 8) + b"}\n")
    assert not any(m["type"] == "response" for m in host.quiet(0.5))
    assert host.response(host.command("state"))["ok"]  # Still serving after every rejection.


def test_close_shuts_down_everything_the_worker_created(host):
    preset = host.command("preset", {"scenario": "travel"})
    state = host.response(preset)["state"]
    assert state["page"]["url"] == "http://127.0.0.1:9/fixture.html?scenario=travel"
    closed = host.response(host.command("close"))
    assert closed["ok"] and closed["state"]["status"] == "idle"
    host.server.join(5)
    assert not host.server.is_alive()
    assert host.dedicated.closed and host.fixtures.closed and host.browsers[0].closed


def test_auto_reports_progress_and_never_treats_done_as_proof(host):
    host.start()
    auto = host.command("auto")
    host.answer(host.ai_request(), "WAIT")
    host.answer(host.ai_request(), "DONE")
    final = host.response(auto)
    assert final["ok"] and final["state"]["status"] == "done"
    assert final["state"]["verification"]["passed"] is None  # A custom goal has no independent check.
    assert any(m["type"] == "event" and m["state"]["busy"] == "auto" for m in host.seen)
    assert host.browsers[0].acts == [("wait", None)]


@pytest.mark.parametrize(("goal", "passed"), [(None, True), ("Open any stay.", None)])
def test_unedited_preset_done_runs_the_independent_check(goal, passed):
    text = "Casa Flora\nYour filters: Design · Free cancellation enabled · Destination Lisbon"
    host = Host(browser_text=text)
    try:
        body = {"scenario": "travel", **({"goal": goal} if goal else {})}
        assert host.response(host.command("preset", body))["ok"]
        host.browsers[0].url = "http://127.0.0.1:9/fixture.html?scenario=travel#casa-flora"
        auto = host.command("auto")
        host.answer(host.ai_request(), "DONE")
        assert host.response(auto)["state"]["verification"]["passed"] is passed
    finally:
        host.close()


def test_a_stop_wakes_a_pending_ai_wait_immediately():
    sent = []
    ai = w.HostAI(type("Channel", (), {"send": lambda self, message: sent.append(message)})())
    token = RunToken()
    threading.Timer(0.1, token.stop).start()
    started = time.monotonic()
    with pytest.raises(Stopped):
        ai.decide({"type": "text-decide"}, timeout_ms=60_000, token=token)
    assert time.monotonic() - started < 2
    assert not ai.deliver(sent[0]["id"], True, {})  # Late results are discarded.


def test_outgoing_text_is_always_strict_utf8():
    text = w.dumps({"text": "ok \ud800 中文 🚀"})
    assert text.encode("utf-8", "strict").decode("utf-8") == '{"text":"ok \ufffd 中文 🚀"}'


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("OPERATION_ABORTED", "canceled"),
        ("OPERATION_TIMEOUT", "ai_timeout"),
        ("AI_INPUT_INVALID", "ai_request_rejected"),
        ("SDK_LOCAL_APP_INPUT_INVALID", "ai_request_rejected"),
        ("AI_OUTPUT_INVALID", "ai_error"),
        ("AI_ROUTE_UNSUPPORTED", "ai_error"),
        ("AI_LOCAL_CONFIGURATION_NOT_CONFIGURED", "ai_error"),
        ("AI_PROVIDER_UNAVAILABLE", "ai_error"),
        ("A_CODE_NOBODY_HAS_SEEN", "ai_error"),
    ],
)
def test_host_error_codes_fail_closed_by_kind(host, code, expected):
    host.start()
    tick = host.command("tick")
    request = host.ai_request()
    error = {"code": code, "message": "Host says no"}
    host.send({"type": "ai_result", "id": request["id"], "ok": False, "error": error})
    failed = host.response(tick)
    assert failed["ok"] is False and failed["error"]["code"] == expected and code in failed["error"]["message"]
    state = host.response(host.command("state"))["state"]
    # A canceled request is discarded like a stop; the next decision observes the page again.
    assert state["status"] == ("stopped" if code == "OPERATION_ABORTED" else "ready")
    assert host.browsers[0].acts == []


def test_input_limit_is_a_blocked_step_with_its_reason(host):
    host.start()
    tick = host.command("tick")
    request = host.ai_request()
    error = {"code": "AI_INPUT_LIMIT_EXCEEDED", "message": "Context window exceeded"}
    host.send({"type": "ai_result", "id": request["id"], "ok": False, "error": error})
    response = host.response(tick)
    assert response["ok"] is True  # Not a crash: the run is blocked, with the reason.
    state = response["state"]
    assert state["status"] == "blocked"
    assert "cannot take this whole page (AI_INPUT_LIMIT_EXCEEDED)" in state["blocked_reason"]
    assert state["decision"] is None and host.browsers[0].acts == []


def test_input_limit_during_text_generation_types_nothing(host):
    host.start()
    predict = host.command("predict")
    host.answer(host.ai_request(), "TYPE_TEXT")
    act = host.command("act", {"fingerprint": host.response(predict)["state"]["page"]["fingerprint"]})
    request = host.ai_request()
    error = {"code": "AI_INPUT_LIMIT_EXCEEDED", "message": "Too long"}
    host.send({"type": "ai_result", "id": request["id"], "ok": False, "error": error})
    assert host.response(act)["state"]["status"] == "blocked"
    assert host.browsers[0].acts == []
