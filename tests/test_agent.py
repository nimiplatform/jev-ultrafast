"""Offline contracts for the two-call operation/target policy. No models, no network, no browser."""

import json
import math
import time
from copy import deepcopy
from datetime import date
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.agent import RunToken, Stopped
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint
from jev_ultrafast.questions import MAX_DECISION_CALLS, NEXT_ACTION, TARGET, TEXT_VALUE
from jev_ultrafast.scenarios import presets, verify_flights


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def result(question_id, ids, selected, probabilities=None):
    probabilities = probabilities or {i: float(i == selected) for i in ids}
    return {
        "type": "text-decide",
        "answers": [
            {
                "questionId": question_id,
                "kind": "choice",
                "selectedCandidateId": selected,
                "probabilities": [{"candidateId": i, "probability": p} for i, p in probabilities.items()],
            }
        ],
        "traceId": f"trace-{question_id}",
    }


class ScriptedAI:
    """A Host stand-in: answers each text.decide question by id and records every request. Never a model."""

    def __init__(self, choices=None, text=None, during_call=None):
        self.choices = choices or {}
        self.text = text
        self.during_call = during_call
        self.decide_calls, self.timeouts, self.text_calls = [], [], []

    def decide(self, spec, *, timeout_ms, token=None):
        self.decide_calls.append(spec)
        self.timeouts.append(timeout_ms)
        if self.during_call:
            self.during_call(len(self.decide_calls))
        question = spec["questions"][0]
        return result(question["id"], [c["id"] for c in question["candidates"]], self.choices[question["id"]])

    def generate_text(self, spec, *, timeout_ms, token=None):
        self.text_calls.append(spec)
        if self.during_call:
            self.during_call(len(self.text_calls))
        return {"type": "text-generate", "text": self.text}


def decision(action="e1", **changes):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "target_decision": "single_candidate",
        "calls": 1,
        "operation_probability": 1.0,
        "target_probability": None,
        "probabilities": {},
        "latency_ms": 10,
        "operation_latency_ms": 10,
        "target_latency_ms": None,
        **changes,
    }


# ----- validation of decision results ------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "extra_id", "missing_id", "duplicate_id", "nan", "bool", "string", "negative", "non_max", "sum",
     "extra_key", "wrong_kind", "not_a_list"],
)
def test_invalid_choice_is_rejected(mutation):
    answer = result("q", ["a", "b"], "a")["answers"][0]
    probabilities = answer["probabilities"]
    if mutation == "unknown":
        answer["selectedCandidateId"] = "invented"
    elif mutation == "extra_id":
        probabilities.append({"candidateId": "c", "probability": 0.0})
    elif mutation == "missing_id":
        del probabilities[1]
    elif mutation == "duplicate_id":
        probabilities[1] = {"candidateId": "a", "probability": 0.0}
    elif mutation == "nan":
        probabilities[1]["probability"] = math.nan
    elif mutation == "bool":
        probabilities[0]["probability"] = True
    elif mutation == "string":
        probabilities[0]["probability"] = "1"
    elif mutation == "negative":
        probabilities[0]["probability"], probabilities[1]["probability"] = 1.5, -0.5
    elif mutation == "non_max":
        answer["selectedCandidateId"] = "b"
    elif mutation == "sum":
        probabilities[0]["probability"] = 0.5
    elif mutation == "extra_key":
        answer["confidence"] = 1.0
    elif mutation == "wrong_kind":
        answer["kind"] = "boolean"
    else:
        answer["probabilities"] = {"a": 1.0, "b": 0.0}
    with pytest.raises(model.InvalidDecision, match="Invalid text.decide"):
        model.validate_choice(answer, ["a", "b"])


@pytest.mark.parametrize(
    ("probabilities", "valid"),
    [({"a": 0.49, "b": 0.49, "c": 0}, True), ({"a": 0.51, "b": 0.51, "c": 0}, True),
     ({"a": 0.49, "b": 0.48, "c": 0}, False), ({"a": 0.52, "b": 0.51, "c": 0}, False)],
)
def test_choice_sums_of_exactly_098_and_102_are_inside_the_tolerance(probabilities, valid):
    answer = result("q", ["a", "b", "c"], "a", probabilities)["answers"][0]
    if valid:
        assert model.validate_choice(answer, ["a", "b", "c"])[0] == "a"
    else:
        with pytest.raises(model.InvalidDecision):
            model.validate_choice(answer, ["a", "b", "c"])


def test_valid_choice_keeps_the_submitted_probabilities():
    selected, probabilities = model.validate_choice(
        result("q", ["a", "b"], "b", {"b": 0.7, "a": 0.3})["answers"][0], ["a", "b"]
    )
    assert selected == "b" and probabilities == {"a": 0.3, "b": 0.7}


@pytest.mark.parametrize("mutation", ["question", "two_answers", "type", "extra_field", "trace_type"])
def test_invalid_result_envelope_is_rejected(mutation):
    question = {"id": "operation", "candidates": {"WAIT": "", "DONE": ""}}
    response = result("operation", ["WAIT", "DONE"], "WAIT")
    if mutation == "question":
        response["answers"][0]["questionId"] = "click_target"
    elif mutation == "two_answers":
        response["answers"].append(deepcopy(response["answers"][0]))
    elif mutation == "type":
        response["type"] = "text-generate"
    elif mutation == "extra_field":
        response["usage"] = {}
    else:
        response["traceId"] = 7
    with pytest.raises(model.InvalidDecision):
        model.decision_result(response, question)


# ----- two-phase choose -------------------------------------------------------------------------------


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


@pytest.mark.parametrize(("operation", "choice"), [("WAIT", "wait"), ("DONE", "DONE"), ("BLOCKED", "BLOCKED")])
def test_operations_without_a_target_make_one_call(operation, choice):
    ai = ScriptedAI({"operation": operation})
    d = model.choose(page(), "Find a book", [], ai)
    assert len(ai.decide_calls) == 1 and d["calls"] == 1
    assert d["choice"] == choice and d["target"] is None and d["target_decision"] is None
    assert d["probabilities"] == {choice: 1.0}
    assert set(d["request"]) == {"operation"}


def test_scroll_needs_no_target_question():
    p = page()
    p["actions"].append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560})
    ai = ScriptedAI({"operation": "SCROLL_DOWN"})
    assert model.choose(p, "Read more", [], ai)["choice"] == "scroll_down"
    assert len(ai.decide_calls) == 1


def test_targeted_operation_asks_one_target_question_on_the_same_state():
    ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    d = model.choose(page(), "Find a book", [], ai)
    assert [s["questions"][0]["id"] for s in ai.decide_calls] == ["operation", "click_target"]
    operation, target = ai.decide_calls
    assert operation["type"] == target["type"] == "text-decide"
    assert operation["state"] == target["state"]  # One observation serves both questions.
    assert [c["id"] for c in target["questions"][0]["candidates"]] == ["1", "2"]  # CLICK targets only.
    assert operation["questions"][0]["instructions"] == {"json": {"goal": "Find a book", "rules": NEXT_ACTION}}
    assert target["questions"][0]["instructions"] == {
        "json": {"goal": "Find a book", "operation": "CLICK", "rules": [NEXT_ACTION, TARGET]}
    }
    assert operation["questions"][0]["candidates"][0]["description"] == {"text": model.OPERATION_LABELS["TYPE_TEXT"]}
    assert d["choice"] == "e3" and d["operation"] == "CLICK" and d["target"] == "2"
    assert d["calls"] == 2 and d["target_decision"] == "model"
    assert d["probabilities"] == {"e2": 0.0, "e3": 1.0} and d["target_probability"] == 1.0
    assert d["operation_latency_ms"] >= 0 and d["target_latency_ms"] >= 0
    assert d["trace_ids"] == ["trace-operation", "trace-click_target"]


def test_target_question_gets_only_the_remaining_decision_budget():
    ai = ScriptedAI({"operation": "CLICK", "click_target": "2"}, during_call=lambda n: time.sleep(0.05 * (n == 1)))
    model.choose(page(), "Find a book", [], ai)
    assert ai.timeouts[0] <= model.DECIDE_TIMEOUT_MS
    assert ai.timeouts[1] <= ai.timeouts[0] - 40


def test_single_target_candidate_needs_no_model_question():
    ai = ScriptedAI({"operation": "TYPE_TEXT"})
    d = model.choose(page(), "Find a book", [], ai)
    assert len(ai.decide_calls) == 1 and d["calls"] == 1
    assert d["choice"] == "e1" and d["target"] == "1" and d["target_decision"] == "single_candidate"
    # No probability is invented for a choice no model made.
    assert d["target_probabilities"] == {} and d["probabilities"] == {} and d["target_probability"] is None


def test_second_call_is_skipped_when_stopped_after_the_operation_answer():
    token = RunToken()
    ai = ScriptedAI({"operation": "CLICK", "click_target": "2"}, during_call=lambda n: token.stop())
    with pytest.raises(Stopped):
        model.choose(page(), "Find a book", [], ai, token=token, before_call=lambda phase: token.check())
    assert len(ai.decide_calls) == 1


def test_click_cannot_consume_an_invented_target():
    ai = ScriptedAI({"operation": "CLICK", "click_target": "999"})
    with pytest.raises(model.InvalidDecision, match="Invalid text.decide"):
        model.choose(page(), "Find a book", [], ai)


def test_target_question_receives_control_state_and_full_next_step_rules():
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })
    ai = ScriptedAI({"operation": "CLICK", "click_target": "3"})
    assert model.choose(p, "Search with free cancellation", [], ai)["choice"] == "e3"
    target = ai.decide_calls[1]["questions"][0]
    first = target["candidates"][0]["description"]["json"]
    assert first["checked"] == "true" and first["selected"] is False
    assert NEXT_ACTION in target["instructions"]["json"]["rules"]


def test_oversized_observation_fails_before_submission():
    p = page()
    p["actions"][2]["label"] = "Go " + "x" * 9000  # One target description over the 8 KiB criterion bound.
    ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    with pytest.raises(model.InvalidDecision, match="exceeds text.decide limits"):
        model.choose(p, "Find a book", [], ai)
    assert len(ai.decide_calls) == 1  # The target request was never sent.


# ----- text helper -------------------------------------------------------------------------------------


def test_quoted_task_text_still_uses_the_text_model():
    ai = ScriptedAI(text='{"text":"Zurich"}')
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context, ai)[0] == "Zurich"
    spec = ai.text_calls[0]
    assert spec["type"] == "text-generate" and spec["systemPrompt"] == TEXT_VALUE
    assert json.loads(spec["input"][0]["content"])["goal"] == 'Fly from "Zurich" to London'
    schema = spec["responseFormat"]["jsonSchema"]
    assert spec["responseFormat"]["kind"] == "json_schema"
    assert schema["properties"]["text"] == {"type": ["string", "null"], "maxLength": 2000}


@pytest.mark.parametrize(
    "content",
    ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}', '{"text":"  "}',
     json.dumps({"text": "x" * 2001}), "[]", None],
)
def test_text_helper_rejects_invalid_values(content):
    with pytest.raises(model.InvalidDecision, match="nothing typed"):
        model.field_text({"goal": "Find a flight"}, ScriptedAI(text=content))


def test_text_helper_rejects_an_unexpected_result_envelope():
    ai = Mock(generate_text=Mock(return_value={"type": "text-generate", "text": '{"text":"Zurich"}', "usage": {}}))
    with pytest.raises(model.InvalidDecision, match="nothing typed"):
        model.field_text({"goal": "Find a flight"}, ai)


# ----- the agent loop ----------------------------------------------------------------------------------


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.ai = ScriptedAI(text='{"text":"book"}')
    a.token = RunToken()
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "decision_calls": 0,
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
        "plan_index": 0,
    }
    return a


def test_predict_records_call_counts_and_timing(runner):
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    runner.command("predict")
    recorded = runner.state["decisions"][-1]
    assert runner.state["decision_calls"] == 2 and recorded["calls"] == 2
    assert {"latency_ms", "operation_latency_ms", "target_latency_ms", "elapsed_ms"} <= set(recorded)
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["decision_calls"] == 2


def test_stop_during_the_operation_call_prevents_the_target_call(runner):
    token = RunToken()
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"}, during_call=lambda n: token.stop())
    with pytest.raises(Stopped):
        runner.command("predict", {}, token)
    assert len(runner.ai.decide_calls) == 1
    assert runner.state["decision"] is None and runner.state["decision_calls"] == 1
    runner.state["browser"].act.assert_not_called()


def test_every_call_draws_on_the_run_budget(runner):
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    runner.state["decision_calls"] = MAX_DECISION_CALLS - 1
    snapshot = runner.command("predict")
    # The exhausted budget ends the run with its reason instead of failing every retry.
    assert snapshot["status"] == "blocked" and "text.decide calls" in snapshot["blocked_reason"]
    assert len(runner.ai.decide_calls) == 1 and runner.state["decision"] is None


def test_the_action_budget_ends_the_run_with_its_reason(runner):
    runner.state["history"] = [{"page_changed": True, "kind": "click"}] * loop.MAX_STEPS
    snapshot = runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert snapshot["status"] == "blocked" and "action run budget" in snapshot["blocked_reason"]
    runner.state["browser"].act.assert_not_called()


def test_stop_during_text_generation_types_nothing(runner):
    token = RunToken()
    runner.ai = ScriptedAI(text='{"text":"book"}', during_call=lambda n: token.stop())
    with pytest.raises(Stopped):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]}, token)
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None and runner.state["history"] == []


def test_null_text_types_nothing(runner):
    runner.ai = ScriptedAI(text='{"text":null}')
    with pytest.raises(model.InvalidDecision, match="nothing typed"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()


def test_continuing_after_a_stop_observes_again(runner):
    runner.mark_stopped()
    assert runner.state["status"] == "stopped" and runner.state["decision"] is None
    runner.ai = ScriptedAI({"operation": "WAIT"})
    runner.command("predict")
    runner.state["browser"].observe.assert_called_once()
    assert runner.state["status"] == "predicted"


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "text.generate", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "text.generate", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait", operation="WAIT", target=None, probabilities={"wait": 1.0})
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_three_unchanged_actions_stop_with_their_reason(runner):
    for _ in range(3):
        runner.state["decision"] = decision("e3", operation="CLICK", target="2", probabilities={"e2": 0.0, "e3": 1.0})
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["blocked_reason"] == "the last three actions did not change the page"


def test_decisions_that_keep_going_stale_stop_with_their_reason(runner):
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    runner.state["browser"].act.side_effect = StalePage("changed")
    for _ in range(loop.MAX_STALE_TICKS):
        runner.command("tick")
    assert runner.state["status"] == "blocked" and runner.state["history"] == []
    assert runner.state["blocked_reason"] == "the page kept changing before the chosen action could run"


def test_an_executed_action_resets_the_stale_count(runner):
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    stale = StalePage("changed")
    runner.state["browser"].act.side_effect = [stale, stale, None, stale, stale]
    for _ in range(5):
        runner.command("tick")
    assert runner.state["status"] == "ready" and len(runner.state["history"]) == 1


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3", operation="CLICK", target="2", probabilities={"e2": 0.0, "e3": 1.0})
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


# ----- browser guards (unchanged semantics) ------------------------------------------------------------


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


def test_browser_refuses_to_run_without_a_dedicated_daemon():
    from jev_ultrafast import harness

    if harness._bound:
        pytest.skip("Browser Harness is already bound in this process")
    with pytest.raises(harness.HarnessError, match="No dedicated automation browser"):
        harness.cdp("Target.getTargets")


# ----- presets and independent checks ------------------------------------------------------------------


def test_flights_preset_uses_a_future_date_regardless_of_locale():
    goal = presets(date(2026, 9, 24))["flights"]["goal"]
    assert "October 24, 2026" in goal and "September 20" not in goal
    assert presets(date(2026, 12, 15))["flights"]["goal"].count("January 14, 2027") == 1


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    day = date(2026, 10, 24)  # A Saturday.
    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-10-24",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sat, Oct 24"),
                ("Nonstop flight on Saturday, October 24. Select flight", ""),
            ]
        ],
    }
    assert verify_flights(actual, day)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify_flights(actual, day)["passed"]


def test_an_observation_too_large_for_text_decide_blocks_the_run_with_its_reason(runner):
    runner.state["page"]["actions"][2]["label"] = "Go " + "x" * 9000
    runner.ai = ScriptedAI({"operation": "CLICK", "click_target": "2"})
    snapshot = runner.command("predict")
    assert snapshot["status"] == "blocked" and "exceeds text.decide limits" in snapshot["blocked_reason"]
    assert len(runner.ai.decide_calls) == 1 and snapshot["decision"] is None
    runner.state["browser"].act.assert_not_called()
