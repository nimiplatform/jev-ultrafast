"""Nimi App worker: the Jev browser loop behind a bounded stdin/stdout protocol.

Run as ``python -m jev_ultrafast.worker``. Every message is one JSON object on one line, strict UTF-8,
at most 4 MiB. stdout carries protocol messages only; stderr carries logs. Unknown types or fields are
rejected and nothing they ask for runs.

Host -> worker
  {"type":"command","id":N,"name":NAME,"body":{...}}      NAME: start preset predict act tick auto stop state close
  {"type":"ai_result","id":M,"ok":true,"result":{...}}
  {"type":"ai_result","id":M,"ok":false,"error":{"code":"...","message":"..."}}
Worker -> Host
  {"type":"response","id":N,"ok":true,"state":{...}}
  {"type":"response","id":N,"ok":false,"error":{"code":"...","message":"..."}}
  {"type":"ai_request","id":M,"kind":"decide"|"generate_text","spec":{...},"timeoutMs":T}
  {"type":"event","name":"state","state":{...}}

Commands run one at a time; another command while one runs gets error "busy". "stop" and "state" are
answered at once: stop invalidates the running decision or auto loop, discards any ai_result that arrives
later and never executes it. Browser input that already started is not rolled back; a later
predict/tick/auto observes the page again first. An event carries the state at each command's start and
end, after each auto step, and after a stop.

Command error codes: invalid_command, busy, stopped, canceled, stale_page, ai_error, ai_timeout,
ai_request_rejected, decision_contract, browser_unavailable, browser_error, rejected, internal_error.
ai_result error codes are handled generically and all fail closed: OPERATION_ABORTED is a cancel (the
result is discarded, nothing executes, the run shows as stopped); OPERATION_TIMEOUT a timeout;
AI_INPUT_INVALID and SDK_LOCAL_APP_INPUT_INVALID a rejected request; AI_INPUT_LIMIT_EXCEEDED blocks the run
with that reason (the command itself succeeds); any other code (AI_OUTPUT_INVALID, AI_ROUTE_UNSUPPORTED,
AI_LOCAL_CONFIGURATION_NOT_CONFIGURED, AI_PROVIDER_*, ...) is an ai_error carrying the code.
"""

import json
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from urllib.parse import urlsplit

from . import harness, scenarios
from .agent import Agent, RunToken, Stopped
from .browser import Browser, StalePage
from .chrome import BrowserSetupError, DedicatedBrowser
from .fixtures import FixtureServer
from .model import (
    AICanceled,
    AIInputTooLarge,
    AIRequestFailed,
    AIRequestRejected,
    AITimeout,
    InvalidDecision,
    ai_failure,
)
from .questions import MAX_DECISION_CALLS, MAX_STEPS

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_ID = 2**53 - 1
MAX_GOAL_CHARS = 2000
MAX_URL_CHARS = 2048
MAX_ERROR_CODE_CHARS = 128
MAX_ERROR_MESSAGE_CHARS = 4000
AI_GRACE_SECONDS = 5
CLOSE_WAIT_SECONDS = 15
PACE_SECONDS = 0.45
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
SURROGATES = re.compile("[\ud800-\udfff]")
# name -> (required body fields, optional body fields)
COMMANDS = {
    "start": ({"url", "goal"}, set()),
    "preset": ({"scenario"}, {"goal"}),
    "predict": (set(), set()),
    "act": ({"fingerprint"}, set()),
    "tick": (set(), set()),
    "auto": (set(), {"pace"}),
    "stop": (set(), set()),
    "state": (set(), set()),
    "close": (set(), set()),
}
PAGE_VIEW = ("url", "title", "w", "h", "text", "scroll", "actions", "fingerprint", "omitted_actions", "screenshot")
UNCHECKED = "No independent check exists for a custom or edited goal; DONE is only the model's claim."


class ProtocolError(ValueError):
    """A message broke the protocol. It is rejected and nothing it asked for runs."""


class MessageTooLarge(ProtocolError):
    """A message exceeds the 4 MiB bound."""


def log(message):
    print(f"jev-worker: {message}", file=sys.stderr, flush=True)


# ----- framing -----------------------------------------------------------------------------------------


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _constant(name):
    raise ProtocolError(f"non-standard JSON value {name}")


def _reject_surrogates(value):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if SURROGATES.search(item):
                raise ProtocolError("unpaired surrogate escape in a string")
        elif isinstance(item, dict):
            stack.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def decode(line):
    """One inbound line -> message dict. Strict UTF-8, strict JSON, one object, no duplicate keys."""
    if len(line) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge("message exceeds 4 MiB")
    try:
        text = line.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ProtocolError("message is not valid UTF-8") from None
    try:
        message = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except json.JSONDecodeError as error:
        raise ProtocolError(f"message is not JSON ({error.msg})") from None
    except RecursionError:
        raise ProtocolError("message is nested too deeply") from None
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    _reject_surrogates(message)
    return message


def dumps(value):
    """Compact JSON text. Page content can carry unpaired surrogates; they become U+FFFD so that the
    output is always valid strict UTF-8."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return SURROGATES.sub("\ufffd", text)


def read_lines(stream):
    """Yield inbound lines without their terminator; None stands for a line over the bound (discarded)."""
    while True:
        line = stream.readline(MAX_MESSAGE_BYTES + 2)
        if not line:
            return
        if not line.endswith(b"\n") and len(line) >= MAX_MESSAGE_BYTES + 2:
            while line and not line.endswith(b"\n"):
                line = stream.readline(1 << 16)
            yield None
            continue
        yield line.rstrip(b"\r\n")


class Channel:
    """Serialized writes of whole lines to the Host."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    def send_text(self, text):
        data = text.encode("utf-8", "strict") + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise MessageTooLarge(f"outgoing message would be {len(data)} bytes")
        with self._lock:
            self._stream.write(data)
            self._stream.flush()

    def send(self, message):
        self.send_text(dumps(message))


# ----- validation --------------------------------------------------------------------------------------


def _valid_id(value):
    return type(value) is int and 0 < value <= MAX_ID


def check_goal(value):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= MAX_GOAL_CHARS:
        raise ProtocolError("goal must contain 1-2,000 characters")
    return value.strip()


def check_url(value):
    """The start URL is only a navigation target: http(s), a host, no embedded credentials."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_URL_CHARS:
        raise ProtocolError("url must be 1-2,048 characters")
    if value != value.strip() or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError("url must not contain spaces or control characters")
    try:
        parts = urlsplit(value)
        _ = parts.port  # Raises for a malformed or out-of-range port.
    except ValueError:
        raise ProtocolError("url is malformed") from None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ProtocolError("url must be an http:// or https:// address")
    if parts.username is not None or parts.password is not None:
        raise ProtocolError("url must not contain credentials")
    return value


def parse_command(message):
    """Validate a command envelope and body. Returns (name, body)."""
    unknown = set(message) - {"type", "id", "name", "body"}
    if unknown:
        raise ProtocolError(f"unknown command fields {sorted(unknown)}")
    name = message.get("name")
    if name not in COMMANDS:
        raise ProtocolError(f"unknown command {name!r}")
    body = message.get("body", {})
    if not isinstance(body, dict):
        raise ProtocolError("body must be an object")
    required, optional = COMMANDS[name]
    if extra := set(body) - required - optional:
        raise ProtocolError(f"unknown {name} fields {sorted(extra)}")
    if missing := required - set(body):
        raise ProtocolError(f"missing {name} fields {sorted(missing)}")
    if name == "start":
        body = {"url": check_url(body["url"]), "goal": check_goal(body["goal"])}
    elif name == "preset":
        if body["scenario"] not in scenarios.presets():
            raise ProtocolError("scenario must be travel, research or flights")
        body = {"scenario": body["scenario"], **({"goal": check_goal(body["goal"])} if "goal" in body else {})}
    elif name == "act":
        if not isinstance(body["fingerprint"], str) or not FINGERPRINT.fullmatch(body["fingerprint"]):
            raise ProtocolError("fingerprint must be the observed page's 64-character hex fingerprint")
    elif name == "auto" and "pace" in body and type(body["pace"]) is not bool:
        raise ProtocolError("pace must be true or false")
    return name, body


def parse_ai_result(message):
    """Validate an ai_result. Returns (id, ok, payload)."""
    ok = message.get("ok")
    if type(ok) is not bool:
        raise ProtocolError("ai_result ok must be true or false")
    expected = {"type", "id", "ok", "result" if ok else "error"}
    if set(message) != expected:
        raise ProtocolError(f"ai_result fields must be exactly {sorted(expected)}")
    if not _valid_id(message["id"]):
        raise ProtocolError("ai_result id must be an integer from 1 to 2^53-1")
    if ok:
        if not isinstance(message["result"], dict):
            raise ProtocolError("ai_result result must be an object")
        return message["id"], True, message["result"]
    error = message["error"]
    if (
        not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error["code"], str)
        or not isinstance(error["message"], str)
        or not 0 < len(error["code"]) <= MAX_ERROR_CODE_CHARS
        or len(error["message"]) > MAX_ERROR_MESSAGE_CHARS
    ):
        raise ProtocolError("ai_result error must be {code, message} strings")
    return message["id"], False, error


# ----- AI through the Host -----------------------------------------------------------------------------


class _Pending:
    def __init__(self, token):
        self.token = token
        self.event = threading.Event()
        self.done = False
        self.result = None
        self.error = None


class HostAI:
    """The ``ai`` port backed by the Host: one ai_request per model call, answered by id.

    A wait ends with the result, a Host error, the time budget, or a stop. A stop always wins: the result
    of a stopped request is discarded even if it arrived first, and later results for it are ignored.
    """

    def __init__(self, channel):
        self.channel = channel
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending = {}

    def decide(self, spec, *, timeout_ms, token):
        return self._request("decide", spec, timeout_ms, token)

    def generate_text(self, spec, *, timeout_ms, token):
        return self._request("generate_text", spec, timeout_ms, token)

    def _request(self, kind, spec, timeout_ms, token):
        token.check()
        pending = _Pending(token)
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = pending
        try:
            if not token.watch(pending.event):
                token.check()
            message = {"type": "ai_request", "id": request_id, "kind": kind, "spec": spec, "timeoutMs": timeout_ms}
            try:
                self.channel.send(message)
            except MessageTooLarge:
                raise InvalidDecision("The AI request exceeds the 4 MiB message bound; no action executed.") from None
            answered = pending.event.wait(timeout_ms / 1000 + AI_GRACE_SECONDS)
            token.check()
            if not answered:
                raise AITimeout(f"No {kind} result within {timeout_ms} ms; no action executed.")
            if pending.error is not None:
                raise ai_failure(pending.error["code"], pending.error["message"])
            return pending.result
        finally:
            token.unwatch(pending.event)
            with self._lock:
                self._pending.pop(request_id, None)

    def deliver(self, request_id, ok, payload):
        """Hand a result to its waiting request. False when it is late, duplicated, stopped or unknown."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.done or pending.token.stopped:
                return False
            pending.done = True
            if ok:
                pending.result = payload
            else:
                pending.error = payload
            pending.event.set()
            return True

    def fail(self, request_id, message):
        """A malformed result for a waiting request fails that request instead of leaving it to time out."""
        return self.deliver(request_id, False, {"code": "invalid_ai_result", "message": message})


# ----- session -----------------------------------------------------------------------------------------


class Session:
    """The worker's dedicated Chrome, fixture server and current agent. Only the command thread mutates
    it while a command runs; the reader thread only rotates the stop token."""

    def __init__(self, ai, *, env=None, today=None, open_browser=None, open_fixtures=None, browser_factory=None):
        self.ai = ai
        self.env = os.environ if env is None else env
        self.today = today
        self._open_browser = open_browser or (lambda: DedicatedBrowser.from_env(self.env))
        self._open_fixtures = open_fixtures or FixtureServer
        self._browser_factory = browser_factory or (lambda url: Browser(url, background=False))
        self.token = RunToken()
        self.dedicated = None
        self.fixtures = None
        self.agent = None
        self.scenario = None
        self.checked_preset = None
        self.verification = None
        self._closed = False

    def stop(self):
        token, self.token = self.token, RunToken()
        token.stop()

    def mark_stopped(self):
        if self.agent:
            self.agent.mark_stopped()

    def require_agent(self):
        if not self.agent:
            raise ValueError("Start a run first")
        return self.agent

    def ensure_browser(self):
        if self.dedicated is not None:
            if self.dedicated.healthy():
                return
            log("the dedicated Chrome is gone; starting a new one")
            self._close_dedicated()
        dedicated = self._open_browser()
        try:
            dedicated.start()
            if self._closed:  # The worker closed while Chrome was starting: never keep it.
                raise Stopped("The worker is closing.")
        except BaseException:
            dedicated.close()
            raise
        self.dedicated = dedicated

    def start(self, name, body, token):
        if name == "preset":
            scenario = body["scenario"]
            preset = scenarios.presets(self.today)[scenario]
            goal = body.get("goal", preset["goal"])
            if preset["fixture"]:
                if self.fixtures is None:
                    self.fixtures = self._open_fixtures()
                url = self.fixtures.url(scenario)
            else:
                url = preset["url"]
            checked = preset if goal == preset["goal"] else None
        else:
            scenario, checked, url, goal = None, None, body["url"], body["goal"]
        self.close_agent()
        token.check()
        self.ensure_browser()
        token.check()
        agent = Agent(url, goal, ai=self.ai, browser_factory=self._browser_factory, screenshots=True)
        if self._closed:
            agent.close()
            raise Stopped("The worker is closing.")
        self.agent = agent
        self.scenario, self.checked_preset = scenario, checked

    def command(self, name, body, token):
        self.require_agent().command(name, body, token)
        self.after_command()

    def after_command(self):
        """Run the independent terminal check once a run reports DONE. DONE alone is never proof."""
        agent = self.agent
        if not agent or agent.state["status"] != "done":
            self.verification = None
            return
        if self.verification is not None:
            return
        if not self.checked_preset:
            self.verification = {"scenario": None, "passed": None, "reason": UNCHECKED}
            return
        try:
            self.verification = scenarios.verify(self.scenario, self.checked_preset, agent.browser)
        except Exception as error:
            self.verification = {
                "scenario": self.scenario,
                "passed": False,
                "error": f"The independent check could not read the page: {error}",
            }

    def view(self, busy=None, compact=False):
        base = {
            "busy": busy,
            "presets": {
                name: {k: preset[k] for k in ("label", "goal", "url", "fixture")}
                for name, preset in scenarios.presets(self.today).items()
            },
            "scenario": self.scenario,
            "verification": self.verification,
            "decision_model": "text.decide",
            "text_model": "text.generate",
            "max_steps": MAX_STEPS,
            "max_decision_calls": MAX_DECISION_CALLS,
        }
        if not self.agent:
            idle = {"status": "idle", "goal": None, "page": None, "elements": [], "decision": None}
            return {**base, **idle, "history": [], "decisions": [], "plan": [], "plan_index": 0, "elapsed_ms": 0}
        snapshot = self.agent.snapshot()
        page = snapshot.pop("page")
        decisions = snapshot.pop("decisions")
        for internal in ("observe_before_decision", "started_at", "record"):
            snapshot.pop(internal, None)
        view = {**snapshot, **base}
        view["page"] = {k: page[k] for k in PAGE_VIEW if k in page}
        # The full requests appear once: in the current decision, else in the latest one (shown after DONE).
        # Every other decision keeps its choice, probabilities, call count and timing.
        latest = [] if view.get("decision") else decisions[-1:]
        earlier = decisions[: len(decisions) - len(latest)]
        view["decisions"] = [{k: v for k, v in d.items() if k != "request"} for d in earlier] + latest
        if compact:
            view["page"].pop("screenshot", None)
            view["decisions"] = [{k: v for k, v in d.items() if k != "request"} for d in decisions[-20:]]
            if view.get("decision"):
                view["decision"] = {k: v for k, v in view["decision"].items() if k != "request"}
            view["truncated"] = True
        return view

    def close_agent(self):
        agent, self.agent = self.agent, None
        self.scenario = self.checked_preset = self.verification = None
        if agent:
            try:
                agent.close()
            except Exception as error:
                log(f"closing the previous tab: {error}")

    def _close_dedicated(self):
        dedicated, self.dedicated = self.dedicated, None
        if dedicated:
            try:
                dedicated.close()
            except Exception as error:
                log(f"closing the dedicated Chrome: {error}")

    def close(self):
        self._closed = True
        self.stop()
        self.close_agent()
        self._close_dedicated()
        fixtures, self.fixtures = self.fixtures, None
        if fixtures:
            fixtures.close()


# ----- worker ------------------------------------------------------------------------------------------


def describe(error):
    """(code, message) for a failed command. Never a traceback; internal failures are logged to stderr."""
    for kind, code in (
        (Stopped, "stopped"),
        (AICanceled, "canceled"),
        (StalePage, "stale_page"),
        (AIInputTooLarge, "ai_input_limit"),
        (AIRequestRejected, "ai_request_rejected"),
        (AIRequestFailed, "ai_error"),
        (AITimeout, "ai_timeout"),
        (InvalidDecision, "decision_contract"),
        (BrowserSetupError, "browser_unavailable"),
        (harness.HarnessError, "browser_unavailable"),
        (ValueError, "rejected"),
        ((RuntimeError, TimeoutError, OSError), "browser_error"),
    ):
        if isinstance(error, kind):
            return code, (str(error) or type(error).__name__)[:MAX_ERROR_MESSAGE_CHARS]
    return "internal_error", "The worker failed; nothing runs automatically. Start again to recover."


class Worker:
    def __init__(self, channel, ai, session):
        self.channel = channel
        self.ai = ai
        self.session = session
        self._lock = threading.Lock()
        self._busy = None  # (command id, name, thread)
        self._view_json = None
        self._closing = False
        self._shut = False
        self._publish(None)

    # Output ---------------------------------------------------------------------------------------------

    def _publish(self, busy):
        """Encode the current view once; responses and events embed the same immutable text."""
        budget = MAX_MESSAGE_BYTES - 1024
        text = None
        for compact in (False, True):
            try:
                candidate = dumps(self.session.view(busy=busy, compact=compact))
            except Exception as error:  # A view must always be sendable; fall back to the minimal one.
                log(f"state view failed: {error!r}")
                break
            if len(candidate.encode("utf-8")) <= budget:
                text = candidate
                break
        if text is None:
            agent = self.session.agent
            text = dumps(
                {
                    "busy": busy,
                    "status": agent.state["status"] if agent else "idle",
                    "truncated": True,
                    "history": agent.state["history"] if agent else [],
                }
            )
        self._view_json = text
        return text

    def _write(self, text):
        try:
            self.channel.send_text(text)
        except (OSError, ValueError) as error:
            log(f"could not write to the Host: {error}")

    def _respond_state(self, command_id, view_json):
        self._write('{"type":"response","id":%d,"ok":true,"state":%s}' % (command_id, view_json))

    def _respond_error(self, command_id, code, message):
        error = {"code": code, "message": message}
        self._write(dumps({"type": "response", "id": command_id, "ok": False, "error": error}))

    def _emit_state(self, view_json):
        self._write('{"type":"event","name":"state","state":%s}' % view_json)

    # Input ----------------------------------------------------------------------------------------------

    def serve(self, stream):
        for line in read_lines(stream):
            if line is None:
                log("rejected a message over 4 MiB")
                continue
            if not line.strip():
                continue
            try:
                self.handle(decode(line))
            except ProtocolError as error:
                log(f"rejected message: {error}")
            except Exception as error:  # A handler bug must not end the worker; nothing ran for this message.
                log("".join(traceback.format_exception(error)))
            if self._closing:
                return

    def handle(self, message):
        kind = message.get("type")
        if kind == "ai_result":
            return self._ai_result(message)
        if kind != "command":
            raise ProtocolError(f"unknown message type {kind!r}")
        command_id = message.get("id")
        if not _valid_id(command_id):
            raise ProtocolError("command id must be an integer from 1 to 2^53-1")
        try:
            name, body = parse_command(message)
        except ProtocolError as error:
            return self._respond_error(command_id, "invalid_command", str(error))
        if name == "state":
            with self._lock:
                text = self._view_json if self._busy else self._publish(None)
            return self._respond_state(command_id, text)
        if name == "stop":
            return self._stop(command_id)
        if name == "close":
            self.shutdown()
            self._closing = True
            return self._respond_state(command_id, self._publish(None))
        with self._lock:
            busy = self._busy is not None or self._shut
            if not busy:
                token = self.session.token
                thread = threading.Thread(
                    target=self._run, args=(command_id, name, body, token), name=f"jev-{name}", daemon=True
                )
                self._busy = (command_id, name, thread)
        if busy:
            return self._respond_error(
                command_id, "busy", "A browser step is already running. Stop it or wait for it to finish."
            )
        thread.start()

    def _ai_result(self, message):
        request_id = message.get("id")
        try:
            request_id, ok, payload = parse_ai_result(message)
        except ProtocolError as error:
            # Fail the waiting request closed rather than let it wait out its budget.
            if _valid_id(request_id) and self.ai.fail(request_id, f"Malformed ai_result: {error}"):
                return
            raise
        if not self.ai.deliver(request_id, ok, payload):
            log(f"discarded ai_result {request_id}: stopped, timed out, duplicated or unknown")

    def _stop(self, command_id):
        with self._lock:
            self.session.stop()  # Wakes any wait for an ai_result; its result will be discarded.
            idle = self._busy is None
            if idle:
                self.session.mark_stopped()
                text = self._publish(None)
            else:
                text = self._view_json  # The running command applies the stop when it ends.
        self._respond_state(command_id, text)
        if idle:
            self._emit_state(text)

    # Commands -------------------------------------------------------------------------------------------

    def _run(self, command_id, name, body, token):
        error, halted = None, False
        try:
            self._emit_state(self._publish(name))
            if name in ("start", "preset"):
                self.session.start(name, body, token)
            elif name == "auto":
                self._auto(body, token)
            else:
                self.session.command(name, body, token)
        except Exception as exc:
            error = describe(exc)
            halted = isinstance(exc, (Stopped, AICanceled))  # Both discard results and execute nothing.
            if error[0] == "internal_error":
                log("".join(traceback.format_exception(exc)))
        with self._lock:
            try:
                if token.stopped or halted:
                    self.session.mark_stopped()
                text = self._publish(None)
            finally:
                self._busy = None
        if error:
            self._respond_error(command_id, *error)
        else:
            self._respond_state(command_id, text)
        self._emit_state(text)

    def _auto(self, body, token):
        """Run automatically until DONE, BLOCKED, a stop, an error or the step bound; progress as events."""
        agent = self.session.require_agent()
        for _ in range(MAX_STEPS * 2):
            token.check()
            if body.get("pace"):
                executed = len(agent.state["history"])
                agent.command("predict", {}, token)
                if agent.state["status"] == "blocked":
                    return
                self._emit_state(self._publish("auto"))
                token.sleep(PACE_SECONDS)
                try:
                    agent.command("act", {"fingerprint": agent.state["page"]["fingerprint"]}, token)
                except StalePage:
                    # The pause lets the page change; observe again as a normal tick does.
                    agent.recover_stale(token, executed)
            else:
                agent.command("tick", {}, token)
            self.session.after_command()
            if agent.state["status"] in {"done", "blocked"}:
                return
            self._emit_state(self._publish("auto"))
        # Reaching the bound is an outcome too; never end an automatic run as if it had only paused.
        agent._block(f"stopped after {MAX_STEPS * 2} automatic steps without finishing")

    # Shutdown -------------------------------------------------------------------------------------------

    def shutdown(self):
        """Stop the running command, then close everything the worker created. Idempotent."""
        with self._lock:
            if self._shut:
                return
            self._shut = True
            self.session.stop()
            busy = self._busy
        if busy:
            busy[2].join(CLOSE_WAIT_SECONDS)
        self.session.close()


def _configure_streams():
    """Strict UTF-8 on both protocol streams regardless of the interpreter locale; stray prints go to stderr."""
    sys.stdin.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    protocol_in, protocol_out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    return protocol_in, protocol_out


def _exit_on_signal(*_args):
    raise SystemExit(0)


def main():
    # In a frozen build sys.executable is this worker, and harness.daemon_command() starts the daemon with it.
    if getattr(sys, "frozen", False) and sys.argv[1:3] == ["-m", "browser_harness.daemon"]:
        import runpy

        sys.argv = [sys.argv[0], *sys.argv[3:]]
        runpy.run_module("browser_harness.daemon", run_name="__main__", alter_sys=True)
        return 0
    protocol_in, protocol_out = _configure_streams()
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _exit_on_signal)
    parent = os.environ.get("JEV_RUNTIME_DIR")
    if parent:
        Path(parent).mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="jev-worker-", dir=parent or None))
    # AF_UNIX paths are short on macOS; Windows uses loopback TCP and keeps everything under one directory.
    if sys.platform == "win32":
        endpoints = runtime / "endpoints"
    else:
        endpoints = Path(tempfile.mkdtemp(prefix="jevbh-", dir="/tmp"))
    worker = None
    try:
        harness.configure(runtime / "harness", endpoints)
        channel = Channel(protocol_out)
        ai = HostAI(channel)
        worker = Worker(channel, ai, Session(ai))
        worker.serve(protocol_in)
    except KeyboardInterrupt:
        pass
    finally:
        if worker:
            worker.shutdown()
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.rmtree(endpoints, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
