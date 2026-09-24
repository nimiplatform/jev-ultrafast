// The inspector talks only to an injected transport (provided by the Nimi Host; static/dev-transport.mjs
// is a dev-only fake for tests):
//   window.jevTransport.command(name, body) -> Promise<state>, rejecting with an Error carrying .code
//   window.jevTransport.onState(listener)  -> the worker's state events (start, progress, stop, end)
const $ = (id) => document.getElementById(id);
const transport = window.jevTransport;
let state = null,
  busy = null,
  stopping = false,
  customUrl = "";
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`
    : "—";
const callCount = (n) => `${n ?? 0} call${n === 1 ? "" : "s"}`;
const targetText = (decision, probability) =>
  decision?.target_decision === "single_candidate" ? "only option" : percent(probability);
async function call(name, body = {}) {
  const data = await transport.command(name, body);
  state = data;
  render();
  return data;
}
function isWebAddress(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && Boolean(url.hostname);
  } catch {
    return false;
  }
}
function showError(message) {
  $("error").textContent = message;
  $("error").hidden = false;
}
function applyScenario() {
  const scenario = $("scenario").value;
  const preset = state?.presets?.[scenario];
  if (scenario === "custom") {
    $("start-url").value = customUrl;
    $("start-url").placeholder = "https://example.com/";
    return;
  }
  $("start-url").value = preset?.url || "";
  $("start-url").placeholder = preset?.fixture ? "Local fixture page served by the worker" : "";
  if (preset) $("goal").value = preset.goal;
}
function controls() {
  const locked = Boolean(busy || state?.busy);
  const live = state?.page && !["done", "blocked"].includes(state.status);
  $("start").disabled = locked;
  $("scenario").disabled = locked;
  $("goal").disabled = locked;
  $("start-url").disabled = locked || $("scenario").value !== "custom";
  $("choose").disabled = locked || !live;
  $("execute").disabled = locked || !state?.decision || !live;
  $("auto").disabled = locked || !live;
  $("auto").hidden = (busy || state?.busy) === "auto";
  $("stop").hidden = !locked;
  $("stop").disabled = stopping;
  $("download").disabled = !state?.history?.length;
}
async function perform(name, fn, label) {
  if (busy || state?.busy) return;
  busy = name;
  stopping = false;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  let failure = null;
  try {
    await fn();
  } catch (error) {
    failure = error;
    try {
      state = await transport.command("state");
    } catch {
      /* Preserve the original failure if the worker disconnected. */
    }
  }
  busy = null;
  stopping = false;
  if (state) render();
  else controls();
  // A stop or a canceled request is not a failure: pending results were discarded and nothing executed.
  if (failure && !["stopped", "canceled"].includes(failure.code)) {
    showError(failure.message || String(failure));
    $("status").textContent = "Paused · needs attention";
  }
}
function render() {
  if (!state) return;
  $("helper").textContent = `Text helper · ${state.text_model}`;
  // A DONE goal gets a check mark only when the independent check passed; otherwise it stays the model's claim.
  const doneMark = state.status === "done" && state.verification?.passed === true ? "✓" : "?";
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? doneMark : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
  const check = state.status === "done" ? state.verification : null;
  const outcome =
    check?.passed === true
      ? "independent check passed"
      : check?.passed === false
        ? "independent check failed"
        : "not independently verified";
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    predicted: "Choice ready · inspect or execute",
    stopped: "Stopped · choose next or run again to observe afresh",
    done: `Model reports DONE · ${outcome}`,
    blocked: state.blocked_reason ? `Blocked · ${state.blocked_reason}` : "Stopped · no supported next action",
  };
  // While a command runs, keep its progress label; a reloaded inspector shows the worker's busy command.
  if (!busy)
    $("status").textContent = state.busy ? `Working · ${state.busy}…` : labels[state.status] || state.status;
  $("verification").hidden = !check;
  $("verification").className = `verification ${check?.passed === true ? "passed" : check?.passed === false ? "failed" : ""}`;
  $("verification").textContent = check
    ? check.passed === null
      ? "DONE is the model's claim"
      : `Independent check ${check.passed ? "passed" : "failed"}`
    : "";
  if (!page) {
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = !page.screenshot;
  if (page.screenshot) $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d ? targetText(d, d.target_probability) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? `Ranked by ${state.decision_model} · ${callCount(d.calls)}` : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probabilities = d?.target_probabilities || {};
  const probability = e => probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  $("history").innerHTML = state.history.length
    ? state.history
        .map(
          (h) =>
            `<div class="trace-row"><span class="number">${String(h.step).padStart(2, "0")}</span><div>${escape(h.action)}${h.text ? ` <b>“${escape(h.text)}”</b><small>${escape(h.text_helper)}</small>` : ""}</div><span class="time">${h.latency_ms} ms · ${callCount(h.decision_calls)} · ${targetText(h, h.probability)}</span><span class="effect">${h.page_changed ? "Page changed" : "No change observed"}</span></div>`,
        )
        .join("")
    : '<p class="muted">Each executed action leaves an observed result.</p>';
  $("step-count").textContent = `${state.history.length} actions · ${(state.elapsed_ms / 1000).toFixed(2)} s`;
  $("model-state").textContent = JSON.stringify(
    d?.request || {
      goal: state.goal,
      url: page.url,
      text: page.text,
      actions: page.actions.map(({ rect, node, ...rest }) => rest),
    },
    null,
    2,
  );
  controls();
}
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const scenario = $("scenario").value,
    goal = $("goal").value;
  if (scenario !== "custom") {
    perform("preset", () => call("preset", { scenario, goal }), "Opening a fresh automation tab…");
    return;
  }
  const url = $("start-url").value.trim();
  if (!isWebAddress(url)) {
    showError("Enter a start URL that begins with http:// or https://.");
    return;
  }
  perform("start", () => call("start", { url, goal }), "Opening the start URL in the automation browser…");
});
$("scenario").addEventListener("change", () => {
  applyScenario();
  controls();
});
$("start-url").addEventListener("input", () => {
  if ($("scenario").value === "custom") customUrl = $("start-url").value;
});
$("choose").addEventListener("click", () =>
  perform("predict", () => call("predict"), "Choosing the operation, then its element…"),
);
$("execute").addEventListener("click", () =>
  perform(
    "act",
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform("auto", () => call("auto", { pace: $("pace").checked }), "Running…"),
);
$("stop").addEventListener("click", () => {
  if (stopping) return;
  stopping = true;
  $("status").textContent = "Stopping · a late model result will be discarded…";
  controls();
  transport.command("stop").then(
    (next) => {
      if (!busy) {
        stopping = false;
        state = next;
        render();
      }
    },
    (error) => showError(error?.message || String(error)),
  );
});
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "jev-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
if (!transport) {
  $("status").textContent = "Cannot reach the local worker";
} else {
  transport.onState((next) => {
    state = next;
    render();
  });
  transport.command("state").then(
    (initial) => {
      state = initial;
      if (state.goal) {
        $("scenario").value = state.scenario || "custom";
        $("goal").value = state.goal;
      } else {
        applyScenario();
      }
      render();
    },
    () => {
      $("status").textContent = "Cannot reach the local worker";
    },
  );
}
