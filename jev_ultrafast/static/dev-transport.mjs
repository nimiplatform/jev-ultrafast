// Dev-only fake of window.jevTransport for renderer tests and offline UI previews. It launches no
// browser and calls no model: every state is canned. index.html never loads it; the Nimi Host provides
// the real transport. Contract: command(name, body) -> Promise<state> (rejecting with an Error carrying
// .code), onState(listener) -> unsubscribe.

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];

function presets() {
  const day = new Date(Date.now() + 30 * 24 * 3600 * 1000);
  return {
    flights: {
      label: "Google Flights · real web",
      url: "https://www.google.com/travel/flights?hl=en",
      fixture: false,
      goal: `Find one-way flights from Zurich to London on ${MONTHS[day.getMonth()]} ${day.getDate()}, ${day.getFullYear()}, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.`,
    },
    travel: { label: "Travel planner · fixture", url: null, fixture: true,
      goal: "Find a Design stay in Lisbon with Free cancellation and open Casa Flora." },
    research: { label: "Reading room · fixture", url: null, fixture: true,
      goal: "Open the article about using finite choices to control browser agents." },
  };
}

const PAGE = {
  url: "http://127.0.0.1:8000/fixture.html?scenario=travel",
  title: "Forma · Find a place to slow down",
  w: 1120,
  h: 780,
  text: "Somewhere you can exhale.\nFind stays",
  scroll: { y: 0, height: 780 },
  fingerprint: "a".repeat(64),
  omitted_actions: 0,
  screenshot: "",
  actions: [
    { id: "e1", kind: "fill", label: "Destination", role: "searchbox", value: "", node: 1,
      rect: { x: 52, y: 230, w: 800, h: 46 } },
    { id: "e2", kind: "click", label: "Open Destination", role: "searchbox", value: "", node: 1,
      rect: { x: 52, y: 230, w: 800, h: 46 } },
    { id: "e3", kind: "click", label: "Find stays", role: "button", value: "", node: 2,
      rect: { x: 866, y: 230, w: 140, h: 46 } },
    { id: "wait", kind: "wait", label: "Wait for the page to update" },
  ],
};
const ELEMENTS = [
  { index: "1", role: "searchbox", value: "", label: "Destination", operations: ["TYPE_TEXT", "CLICK"] },
  { index: "2", role: "button", value: "", label: "Find stays", operations: ["CLICK"] },
];

function decision(singleCandidate) {
  const operation = singleCandidate ? "TYPE_TEXT" : "CLICK";
  return {
    choice: singleCandidate ? "e1" : "e3",
    operation,
    target: singleCandidate ? "1" : "2",
    target_decision: singleCandidate ? "single_candidate" : "model",
    calls: singleCandidate ? 1 : 2,
    operation_probability: 0.8,
    target_probability: singleCandidate ? null : 0.9,
    probabilities: singleCandidate ? {} : { e2: 0.1, e3: 0.9 },
    operation_probabilities: { [operation]: 0.8, WAIT: 0.1, DONE: 0.05, BLOCKED: 0.05 },
    target_probabilities: singleCandidate ? {} : { 1: 0.1, 2: 0.9 },
    latency_ms: singleCandidate ? 120 : 240,
    operation_latency_ms: 118,
    target_latency_ms: singleCandidate ? null : 117,
    trace_ids: [],
    model: "text.decide",
    request: { operation: { type: "text-decide" } },
  };
}

export function createFakeTransport({ singleCandidate = false } = {}) {
  const listeners = new Set();
  const calls = [];
  const held = new Set();
  let pending = [];
  const common = {
    busy: null, presets: presets(), scenario: null, verification: null, decision_model: "text.decide",
    text_model: "text.generate", max_steps: 60, max_decision_calls: 240,
  };
  let state = { ...common, status: "idle", goal: null, page: null, elements: [], decision: null, history: [],
    decisions: [], plan: [], plan_index: 0, elapsed_ms: 0 };
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const fail = (code, message) => Object.assign(new Error(message), { code });
  const emit = () => listeners.forEach((listener) => listener(clone(state)));
  const live = () => {
    if (!state.page) throw fail("rejected", "Start a run first");
  };

  function run(name, body) {
    switch (name) {
      case "state":
        return;
      case "start":
      case "preset": {
        const goal = (body.goal ?? state.presets[body.scenario]?.goal ?? "").trim();
        if (!goal) throw fail("invalid_command", "goal must contain 1-2,000 characters");
        state = { ...state, status: "ready", goal, plan: [goal], plan_index: 0, page: clone(PAGE),
          elements: clone(ELEMENTS), decision: null, history: [], decisions: [], verification: null,
          scenario: name === "preset" ? body.scenario : null };
        return;
      }
      case "predict":
        live();
        state.decision = decision(singleCandidate);
        state.decisions.push(state.decision);
        state.status = "predicted";
        return;
      case "act":
      case "tick": {
        live();
        if (name === "tick") run("predict", {});
        const d = state.decision;
        if (!d) throw fail("rejected", "Observe and choose before acting");
        state.history.push({ step: state.history.length + 1, action: PAGE.actions.find((a) => a.id === d.choice).label,
          kind: "click", choice: d.choice, probability: d.probabilities[d.choice] ?? null,
          target_decision: d.target_decision, decision_calls: d.calls, latency_ms: d.latency_ms, text: null,
          text_helper: null, page_changed: true });
        state.decision = null;
        state.status = "ready";
        return;
      }
      case "auto":
        live();
        run("tick", {});
        emit();
        state.status = "done";
        state.plan_index = 1;
        state.verification = { scenario: null, passed: null, reason: "No independent check exists." };
        return;
      case "close":
        state = { ...state, status: "idle", page: null, goal: null, decision: null };
        return;
      default:
        throw fail("invalid_command", `unknown command ${JSON.stringify(name)}`);
    }
  }

  return {
    calls,
    get state() {
      return clone(state);
    },
    hold(name) {
      held.add(name); // The next `name` command waits until stop.
    },
    onState(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    command(name, body = {}) {
      calls.push({ name, body: clone(body) });
      if (name === "stop") {
        const waiting = pending;
        pending = [];
        if (state.page && !["done", "blocked"].includes(state.status)) state.status = "stopped";
        state.decision = null;
        waiting.forEach(({ reject }) => reject(fail("stopped", "Stopped. Nothing further was executed.")));
        emit();
        return Promise.resolve(clone(state));
      }
      if (held.delete(name)) {
        return new Promise((resolve, reject) => pending.push({ resolve, reject }));
      }
      try {
        run(name, body);
        return Promise.resolve(clone(state));
      } catch (error) {
        return Promise.reject(error);
      }
    },
  };
}

export function installFakeTransport(target = globalThis) {
  target.jevTransport = createFakeTransport();
  return target.jevTransport;
}
