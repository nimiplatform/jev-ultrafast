"""Operation/target decisions and field text, executed by the Host through Nimi.

Python builds bounded business requests and validates every result. It holds no credentials, runs no
model and never sees Runtime endpoints: an ``ai`` port hands each request to the Host, which runs
``text.decide`` for decisions and ``text.generate`` (structured output) for field text. Which backend
answers is Nimi's decision; nothing here knows or branches on it.
"""

import json
import math
import time
import unicodedata

from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

# One decision (operation question plus an optional target question) shares this time budget.
DECIDE_TIMEOUT_MS = 60_000
TEXT_TIMEOUT_MS = 60_000
MIN_TARGET_BUDGET_MS = 1_000
FIELD_TEXT_MAX_CHARS = 2000

# Runtime text.decide request bounds, mirrored so an oversized observation fails before submission.
MAX_STATE_BYTES = 256 * 1024
MAX_INSTRUCTION_BYTES = 32 * 1024
MAX_CRITERION_BYTES = 8 * 1024
MAX_DECIDE_BYTES = 1024 * 1024
MIN_CANDIDATES, MAX_CANDIDATES = 2, 255
MAX_ID_BYTES = 64
PROBABILITY_SUM_TOLERANCE = 0.02
# A binary64 sum of decimal probabilities that is exactly 0.98 or 1.02 lands a few ulps past the bound.
PROBABILITY_SUM_EPSILON = 1e-9
ARGMAX_TOLERANCE = 1e-6

OPERATION_LABELS = {
    "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
    "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
    "SELECT": "Select an observed dropdown value.",
}
TERMINAL_LABELS = {"DONE": "Every requirement is visibly satisfied.", "BLOCKED": "No supported operation can progress."}
FIELD_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": ["string", "null"], "maxLength": FIELD_TEXT_MAX_CHARS}},
    "required": ["text"],
    "additionalProperties": False,
}


class InvalidDecision(ValueError):
    """A request or result broke the decision contract. Nothing executes."""


class ObservationTooLarge(InvalidDecision):
    """The observation does not fit one text.decide request. The run is blocked; nothing executes."""


class AIRequestFailed(RuntimeError):
    """The Host reported that an AI request failed. Nothing executes."""

    def __init__(self, code, message):
        super().__init__(f"{message} ({code}); no action executed.")
        self.code = code


class AIRequestRejected(AIRequestFailed):
    """The request was rejected as invalid (AI_INPUT_INVALID, SDK_LOCAL_APP_INPUT_INVALID)."""


class AIInputTooLarge(AIRequestFailed):
    """AI_INPUT_LIMIT_EXCEEDED: the configured model cannot take this whole page. The run is blocked."""

    def __init__(self, code, message):
        RuntimeError.__init__(self, f"The configured model cannot take this whole page ({code}); nothing executed.")
        self.code = code


class AICanceled(RuntimeError):
    """OPERATION_ABORTED: the request was canceled. Its result is discarded and nothing executes."""


class AITimeout(TimeoutError):
    """No AI result arrived within the request budget. Nothing executes."""


def ai_failure(code, message):
    """The exception for a Host-reported {code, message}. Every code fails closed; unknown codes are generic."""
    if code == "OPERATION_ABORTED":
        return AICanceled(f"The AI request was canceled ({code}); nothing was executed.")
    if code == "OPERATION_TIMEOUT":
        return AITimeout(f"{message} ({code}); no action executed.")
    if code == "AI_INPUT_LIMIT_EXCEEDED":
        return AIInputTooLarge(code, message)
    if code in ("AI_INPUT_INVALID", "SDK_LOCAL_APP_INPUT_INVALID"):
        return AIRequestRejected(code, message)
    return AIRequestFailed(code, message)  # AI_OUTPUT_INVALID, AI_ROUTE_UNSUPPORTED, AI_PROVIDER_*, ...


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def decision_representation(page, goal, history):
    """The one swappable decision representation, built from a single observation.

    State: page url/title/text, the indexed element table and recent actions. The operation question offers
    the operation labels as text; each target question offers that operation's elements with their current
    value and checked/selected/expanded state as JSON. choose() consumes only this output, so a compact
    representation can replace this function uniformly for every backend.
    """
    elements, targets, controls = action_space(page["actions"])
    operations = {key: OPERATION_LABELS[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(TERMINAL_LABELS)
    state = {
        "page": {k: page[k] for k in ("url", "title", "text")},
        "elements": elements,
        "recent_actions": [{k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]],
    }
    questions = {
        "operation": {
            "id": "operation",
            "instructions": {"goal": goal, "rules": NEXT_ACTION},
            "candidates": operations,
        }
    }
    for operation, candidates in targets.items():
        questions[operation] = {
            "id": operation.lower() + "_target",
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
            "candidates": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
        }
    return {"state": state, "questions": questions, "targets": targets, "controls": controls}


def content(value):
    return {"text": value} if isinstance(value, str) else {"json": value}


def decide_spec(state, question):
    """One text.decide request with exactly one choice question (the Nimi SDK TextDecideScenarioSpec shape)."""
    return {
        "type": "text-decide",
        "state": {"json": state},
        "questions": [
            {
                "id": question["id"],
                "kind": "choice",
                "instructions": {"json": question["instructions"]},
                "candidates": [
                    {"id": candidate_id, "description": content(description)}
                    for candidate_id, description in question["candidates"].items()
                ],
            }
        ],
    }


def _json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass"))


def _valid_id(value):
    return (
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value.encode("utf-8", "surrogatepass")) <= MAX_ID_BYTES
        and not any(unicodedata.category(c) in ("Cc", "Cs") or c == "\ufffd" for c in value)
    )


def check_decide_spec(spec):
    """Reject a request the Runtime would refuse, before it leaves the worker."""
    question = spec["questions"][0]
    ids = [candidate["id"] for candidate in question["candidates"]]
    if not MIN_CANDIDATES <= len(ids) <= MAX_CANDIDATES:
        raise InvalidDecision(f"A text.decide question needs 2-255 candidates, not {len(ids)}; no action executed.")
    if len(set(ids)) != len(ids) or not all(_valid_id(i) for i in [question["id"], *ids]):
        raise InvalidDecision("Invalid text.decide question or candidate id; no action executed.")
    problem = None
    if _json_bytes(spec["state"]["json"]) > MAX_STATE_BYTES:
        problem = "page state over 256 KiB"
    elif _json_bytes(question["instructions"]["json"]) > MAX_INSTRUCTION_BYTES:
        problem = "instructions over 32 KiB"
    else:
        for candidate in question["candidates"]:
            description = candidate["description"]
            if "text" in description:
                text = description["text"]
                oversized = len(text.encode("utf-8", "surrogatepass")) > MAX_CRITERION_BYTES
                if oversized or not text.strip() or "\0" in text:
                    problem = f"candidate {candidate['id']} description is empty or over 8 KiB"
                    break
            elif _json_bytes(description["json"]) > MAX_CRITERION_BYTES:
                problem = f"candidate {candidate['id']} description over 8 KiB"
                break
        else:
            if _json_bytes(spec) > MAX_DECIDE_BYTES:
                problem = "request over 1 MiB"
    if problem:
        raise ObservationTooLarge(f"This page exceeds text.decide limits ({problem}); nothing executed.")


def validate_choice(answer, ids):
    """One choice answer: the selected id and probabilities covering exactly the submitted ids.

    Probabilities must be finite numbers in [0, 1], sum to 1 within the Runtime tolerance, and the selected
    candidate must be the most probable one. Nothing is repaired, reordered into truth or invented.
    """
    ids = list(ids)
    try:
        probabilities = answer["probabilities"]
        pairs = [(entry["candidateId"], entry["probability"]) for entry in probabilities]
        mapping = dict(pairs)
        selected = answer["selectedCandidateId"]
        valid = (
            set(answer) == {"questionId", "kind", "selectedCandidateId", "probabilities"}
            and answer["kind"] == "choice"
            and isinstance(probabilities, list)
            and all(isinstance(entry, dict) and set(entry) == {"candidateId", "probability"} for entry in probabilities)
            and len(pairs) == len(mapping) == len(ids)
            and set(mapping) == set(ids)
            and isinstance(selected, str)
            and selected in mapping
            and all(type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1 for p in mapping.values())
            and abs(sum(mapping.values()) - 1) <= PROBABILITY_SUM_TOLERANCE + PROBABILITY_SUM_EPSILON
            and mapping[selected] >= max(mapping.values()) - ARGMAX_TOLERANCE
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise InvalidDecision("Invalid text.decide answer; no action executed.")
    return selected, {candidate_id: float(mapping[candidate_id]) for candidate_id in ids}


def decision_result(result, question):
    """Validate a text.decide result for one submitted question; return (selected id, probabilities)."""
    try:
        valid = (
            isinstance(result, dict)
            and {"type", "answers"} <= set(result) <= {"type", "answers", "traceId"}
            and result["type"] == "text-decide"
            and isinstance(result.get("traceId", ""), str)
            and isinstance(result["answers"], list)
            and len(result["answers"]) == 1
            and isinstance(result["answers"][0], dict)
            and result["answers"][0].get("questionId") == question["id"]
        )
    except TypeError:
        valid = False
    if not valid:
        raise InvalidDecision("Invalid text.decide result; no action executed.")
    return validate_choice(result["answers"][0], question["candidates"])


def choose(page, goal, history, ai, *, token=None, before_call=None, representation=decision_representation):
    """Decide the next operation, then its target, from one observation.

    Ask ONE operation question. Only CLICK, TYPE_TEXT and SELECT need a target: then ask ONE target question
    for that operation with the same state and whatever remains of the same decision budget. WAIT, DONE,
    BLOCKED and scrolling need no second call. A single target candidate is taken without a model question
    and carries no probability. ``before_call(phase)`` runs before each call and must raise to prevent it.
    """
    rep = representation(page, goal, history)
    before_call = before_call or (lambda _phase: None)
    started = time.perf_counter()
    deadline = started + DECIDE_TIMEOUT_MS / 1000
    requests, trace_ids = {}, []

    def ask(phase, question):
        before_call(phase)
        remaining_ms = int((deadline - time.perf_counter()) * 1000)
        if remaining_ms < MIN_TARGET_BUDGET_MS:
            raise AITimeout("The decision budget ran out before the target question; no action executed.")
        spec = decide_spec(rep["state"], question)
        check_decide_spec(spec)
        requests[phase] = spec
        asked = time.perf_counter()
        result = ai.decide(spec, timeout_ms=remaining_ms, token=token)
        latency_ms = round((time.perf_counter() - asked) * 1000)
        selected, probabilities = decision_result(result, question)
        if result.get("traceId"):
            trace_ids.append(result["traceId"])
        return selected, probabilities, latency_ms

    operation, operation_probabilities, operation_latency = ask("operation", rep["questions"]["operation"])
    target, target_probabilities, target_latency, target_decision = None, {}, None, None
    if operation in rep["targets"]:
        question = rep["questions"][operation]
        candidate_ids = list(question["candidates"])
        if len(candidate_ids) == 1:
            # No model question for a single candidate; no probability is reported for it.
            target, target_decision = candidate_ids[0], "single_candidate"
        else:
            target, target_probabilities, target_latency = ask("target", question)
            target_decision = "model"
        candidates = rep["targets"][operation]
        choice = candidates[target]["id"]
        probabilities = {
            a["id"]: target_probabilities[index] for index, a in candidates.items() if target_probabilities
        }
    else:
        choice = rep["controls"][operation]["id"] if operation in rep["controls"] else operation
        probabilities = {choice: operation_probabilities[operation]}
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "target_decision": target_decision,
        "calls": len(requests),
        "operation_probability": operation_probabilities[operation],
        "target_probability": target_probabilities.get(target),
        "probabilities": probabilities,
        "operation_probabilities": operation_probabilities,
        "target_probabilities": target_probabilities,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "operation_latency_ms": operation_latency,
        "target_latency_ms": target_latency,
        "trace_ids": trace_ids,
        "model": "text.decide",
        "request": requests,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def text_spec(context):
    """One text.generate request with structured output {"text": string|null} (TextGenerateScenarioSpec shape)."""
    return {
        "type": "text-generate",
        "systemPrompt": TEXT_VALUE,
        "input": [{"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
        "responseFormat": {
            "kind": "json_schema",
            "schemaName": "jev_field_text",
            "strict": True,
            "jsonSchema": FIELD_TEXT_SCHEMA,
        },
        "maxTokens": 1024,
    }


def field_text(context, ai, *, token=None):
    started = time.perf_counter()
    result = ai.generate_text(text_spec(context), timeout_ms=TEXT_TIMEOUT_MS, token=token)
    try:
        if (
            not isinstance(result, dict)
            or not {"type", "text"} <= set(result) <= {"type", "text", "traceId"}
            or result["type"] != "text-generate"
            or not isinstance(result.get("traceId", ""), str)
        ):
            raise ValueError()
        output = json.loads(result["text"])
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip():
            raise ValueError()
        if len(value) > FIELD_TEXT_MAX_CHARS:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise InvalidDecision("Text helper returned no valid field value; nothing typed.") from None
    return value, {
        "model": "text.generate",
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "trace_id": result.get("traceId"),
    }
