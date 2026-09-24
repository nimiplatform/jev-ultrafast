// Renderer contract checks: app.js against index.html and the dev fake transport. Run: node --test tests/
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

import { createFakeTransport } from "../jev_ultrafast/static/dev-transport.mjs";

const html = readFileSync(new URL("../jev_ultrafast/static/index.html", import.meta.url), "utf8");
const source = readFileSync(new URL("../jev_ultrafast/static/app.js", import.meta.url), "utf8");
const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve));
};

class Element {
  constructor(id) {
    Object.assign(this, { id, textContent: "", innerHTML: "", hidden: false, disabled: false, value: "",
      checked: false, placeholder: "", className: "", src: "", dataset: {}, classList: { toggle() {} } });
    this.listeners = new Map();
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  dispatch(type) {
    for (const listener of this.listeners.get(type) || []) listener({ preventDefault() {}, target: this });
  }
}

async function boot(transport) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element(id)]));
  elements.get("scenario").value = "flights";
  elements.get("overlays").checked = true;
  elements.get("stop").hidden = true;
  const document = {
    getElementById(id) {
      if (!elements.has(id)) throw new Error(`app.js reads #${id}, which index.html does not define`);
      return elements.get(id);
    },
    querySelectorAll: () => [],
    createElement: () => new Element("download-link"),
  };
  const context = vm.createContext({ document, URL, console, jevTransport: transport });
  context.window = context;
  vm.runInContext(source, context, { filename: "app.js" });
  await settle();
  return elements;
}

test("app.js uses only the injected transport, never an HTTP API or token", () => {
  assert.doesNotMatch(source, /fetch\(|\/api\/|X-Demo-Token|demo-token/);
  assert.match(source, /window\.jevTransport/);
});

test("the initial state supplies the presets; flights uses a future date", async () => {
  const transport = createFakeTransport();
  const el = await boot(transport);
  assert.equal(transport.calls[0].name, "state");
  assert.equal(el.get("goal").value, transport.state.presets.flights.goal);
  assert.equal(el.get("start-url").value, "https://www.google.com/travel/flights?hl=en");
  assert.equal(el.get("status").textContent, "Ready to explore");
});

test("presets start through the preset command; a custom start needs an http(s) URL", async () => {
  const transport = createFakeTransport();
  const el = await boot(transport);
  el.get("scenario").value = "travel";
  el.get("scenario").dispatch("change");
  el.get("task-form").dispatch("submit");
  await settle();
  assert.deepEqual(transport.calls.at(-1), {
    name: "preset", body: { scenario: "travel", goal: transport.state.presets.travel.goal },
  });

  el.get("scenario").value = "custom";
  el.get("scenario").dispatch("change");
  assert.equal(el.get("start-url").disabled, false);
  el.get("goal").value = "打开 Wikipedia 首页并找到今日特色条目 🚀";
  el.get("start-url").value = "ftp://example.com/";
  el.get("task-form").dispatch("submit");
  await settle();
  assert.equal(el.get("error").hidden, false);
  assert.notEqual(transport.calls.at(-1).name, "start");

  el.get("start-url").value = "https://www.wikipedia.org/";
  el.get("start-url").dispatch("input");
  el.get("task-form").dispatch("submit");
  await settle();
  assert.deepEqual(transport.calls.at(-1), {
    name: "start", body: { url: "https://www.wikipedia.org/", goal: "打开 Wikipedia 首页并找到今日特色条目 🚀" },
  });
});

test("Stop reaches the worker while a decision waits; a stopped command is not an error", async () => {
  const transport = createFakeTransport();
  const el = await boot(transport);
  el.get("task-form").dispatch("submit");
  await settle();
  transport.hold("predict");
  el.get("choose").dispatch("click");
  await settle();
  assert.equal(el.get("stop").hidden, false);
  assert.equal(el.get("choose").disabled, true);
  el.get("stop").dispatch("click");
  await settle();
  assert.deepEqual(transport.calls.slice(-3).map((c) => c.name), ["predict", "stop", "state"]);
  assert.equal(el.get("error").hidden, true);
  assert.match(el.get("status").textContent, /^Stopped/);
  assert.equal(el.get("execute").disabled, true);
  assert.equal(el.get("choose").disabled, false);
});

test("a single-candidate target shows no invented probability", async () => {
  const transport = createFakeTransport({ singleCandidate: true });
  const el = await boot(transport);
  el.get("task-form").dispatch("submit");
  await settle();
  el.get("choose").dispatch("click");
  await settle();
  assert.equal(el.get("confidence").textContent, "only option");
  assert.match(el.get("ranking-note").textContent, /1 call$/);
  el.get("execute").dispatch("click");
  await settle();
  assert.match(el.get("history").innerHTML, /only option/);
  assert.doesNotMatch(el.get("history").innerHTML + el.get("choices").innerHTML, /NaN/);
});
