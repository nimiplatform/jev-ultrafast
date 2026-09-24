"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import threading
import time
from pathlib import Path

from .browser import Browser, StalePage
from .model import AIInputTooLarge, ObservationTooLarge, action_space, choose, field_context, field_text
from .questions import MAX_DECISION_CALLS, MAX_STEPS

# Automatic ticks in a row whose decision went stale before it could run (the page keeps changing)
# before the run stops with that reason instead of spending its decision budget.
MAX_STALE_TICKS = 3


class Stopped(RuntimeError):
    """The run was stopped. Pending model results are discarded and nothing further executes."""


class DecisionBudgetExhausted(ValueError):
    """The run used its whole text.decide call budget; it ends with that reason."""


class RunToken:
    """Cancellation for one stretch of a run. stop() wakes every wait registered on this token."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stopped = False
        self._event = threading.Event()
        self._watchers = set()

    @property
    def stopped(self):
        return self._stopped

    def stop(self):
        with self._lock:
            self._stopped = True
            watchers, self._watchers = self._watchers, set()
        self._event.set()
        for event in watchers:
            event.set()

    def check(self):
        if self._stopped:
            raise Stopped("Stopped. Nothing further was executed; continue to observe and decide again.")

    def watch(self, event):
        """Register a wait that stop() must wake. False when already stopped."""
        with self._lock:
            if self._stopped:
                return False
            self._watchers.add(event)
            return True

    def unwatch(self, event):
        with self._lock:
            self._watchers.discard(event)

    def sleep(self, seconds):
        self._event.wait(seconds)
        self.check()


class Agent:
    def __init__(self, url, goals, *, ai, browser_factory=Browser, record_dir=None, screenshots=False):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.ai = ai
        self.token = RunToken()
        self.pending_text = None
        self.browser = browser_factory(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            decision_calls=0,
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def stop(self):
        """Invalidate the running decision/loop from any thread. Browser input already started is not undone."""
        token, self.token = self.token, RunToken()
        token.stop()

    def mark_stopped(self):
        """Show a stop in the state while no command runs. The next decision starts with a new observation."""
        state = self.state
        state["decision"] = None
        state["observe_before_decision"] = True
        if state["status"] not in {"done", "blocked"}:
            state["status"] = "stopped"

    def _before_call(self, phase, token):
        # Every text.decide call, including the target question, draws on the same run budget.
        token.check()
        if self.state["decision_calls"] >= MAX_DECISION_CALLS:
            raise DecisionBudgetExhausted(f"reached the run's budget of {MAX_DECISION_CALLS} text.decide calls")
        self.state["decision_calls"] += 1

    def recover_stale(self, token, executed):
        """The chosen action went stale: observe again, or stop when the page keeps changing."""
        state = self.state
        token.check()
        state["decision"] = None
        # Only a decision that went stale before it could run counts; an executed action is progress.
        state["stale_ticks"] = 0 if len(state["history"]) > executed else state.get("stale_ticks", 0) + 1
        if state["stale_ticks"] >= MAX_STALE_TICKS:
            return self._block("the page kept changing before the chosen action could run")
        state["status"] = "ready"
        state["page"] = state["browser"].observe(screenshot=self.screenshots)
        state["elapsed_ms"] = self._elapsed()
        return self.snapshot()

    def _elapsed(self):
        return round((time.perf_counter() - self.state["started_at"]) * 1000)

    def _block(self, reason):
        """A blocked step with its reason (e.g. the model cannot take this page), not a crash."""
        self.state.update(status="blocked", blocked_reason=reason, decision=None, elapsed_ms=self._elapsed())
        return self.snapshot()

    def command(self, name, body=None, token=None):
        body = body or {}
        token = token or self.token
        state = self.state
        if name == "tick":
            executed = len(state["history"])
            try:
                self.command("predict", {}, token)
                if state["status"] == "blocked":
                    return self.snapshot()
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]}, token)
            except StalePage:
                return self.recover_stale(token, executed)
        elif name == "predict":
            token.check()
            if not state["browser"]:
                raise ValueError("Start a run first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            # After a stop, continuing always observes again; otherwise only a changed page is re-read.
            if state.pop("observe_before_decision", False) or not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has finished. Start a fresh run.")
            page = state["page"]
            try:
                decision = choose(
                    page,
                    state["goal"],
                    state["history"],
                    self.ai,
                    token=token,
                    before_call=lambda phase: self._before_call(phase, token),
                )
            except (AIInputTooLarge, ObservationTooLarge, DecisionBudgetExhausted) as error:
                token.check()
                return self._block(str(error))
            token.check()  # A result that raced with a stop is discarded, never offered for execution.
            state["decision"] = decision
            state["decisions"].append({**decision, "fingerprint": page["fingerprint"], "elapsed_ms": self._elapsed()})
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            token.check()
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                token.check()
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["blocked_reason"] = None  # The model's own BLOCKED carries no reason.
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = self._elapsed()
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                return self._block(f"stopped at the {MAX_STEPS}-action run budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    try:
                        text, helper = field_text(context, self.ai, token=token)
                    except AIInputTooLarge as error:
                        token.check()
                        return self._block(str(error))
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Stop wins over a late result: checked immediately before the only mutation.
            token.check()
            # Browser.act checks freshness immediately before input, including after text generation.
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = self._elapsed()
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "probability": decision["probabilities"].get(selected),
                    "operation_probability": decision["operation_probability"],
                    "target_decision": decision["target_decision"],
                    "decision_calls": decision["calls"],
                    "latency_ms": decision["latency_ms"],
                    "operation_latency_ms": decision["operation_latency_ms"],
                    "target_latency_ms": decision["target_latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "executed_ms": self._elapsed(),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            # An executed action is progress on every path (tick, manual Execute, slow motion).
            state["stale_ticks"] = 0
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = self._elapsed()
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated):
                state.update(status="blocked", blocked_reason="the last three actions did not change the page")
            else:
                state["status"] = "ready"
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.token.stop()
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
