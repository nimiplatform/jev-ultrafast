// window.jevTransport over the Kit renderer bridge (renderer/transport.ts), alone and under the unchanged
// app.js. The bridge is a fake of Kit's invoke/listenShell; no Electron or worker. Run: node --test tests/
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

import { createJevTransport, WORKER_COMMAND, WORKER_STATE_EVENT } from '../renderer/transport.ts';

const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve));
};

function fakeBridge(reply) {
  const invocations = [];
  const listeners = new Map();
  return {
    invocations,
    emit: (payload) => listeners.get(WORKER_STATE_EVENT)?.({ payload }),
    listening: () => listeners.has(WORKER_STATE_EVENT),
    invoke: async (command, payload) => {
      invocations.push({ command, payload });
      return reply(payload);
    },
    listen: async (eventName, handler) => {
      listeners.set(eventName, handler);
      return () => listeners.delete(eventName);
    },
  };
}

test('command sends {name, body} as the one App command and resolves the worker state', async () => {
  const bridge = fakeBridge(() => ({ ok: true, state: { status: 'ready', goal: '目标 🚀' } }));
  const transport = createJevTransport(bridge);
  assert.deepEqual(await transport.command('start', { url: 'https://example.com/', goal: '目标 🚀' }), {
    status: 'ready', goal: '目标 🚀',
  });
  assert.deepEqual(await transport.command('state'), { status: 'ready', goal: '目标 🚀' });
  assert.deepEqual(bridge.invocations, [
    { command: WORKER_COMMAND, payload: { name: 'start', body: { url: 'https://example.com/', goal: '目标 🚀' } } },
    { command: WORKER_COMMAND, payload: { name: 'state', body: {} } },
  ]);
});

test('worker and Host failures reject with an Error carrying .code', async () => {
  const stopped = createJevTransport(fakeBridge(() => ({ ok: false, error: { code: 'stopped', message: 'Stopped.' } })));
  await assert.rejects(stopped.command('predict'), (error) => error instanceof Error && error.code === 'stopped');
  const refused = createJevTransport({
    ...fakeBridge(() => null),
    invoke: async () => {
      throw Object.assign(new Error('Unsupported command'), { code: 'host-internal-error', reasonCode: 'worker_exited' });
    },
  });
  await assert.rejects(refused.command('tick'), (error) => error.code === 'worker_exited');
  const invalid = createJevTransport(fakeBridge(() => ({ ok: true })));
  await assert.rejects(invalid.command('state'), (error) => error.code === 'host_protocol_error');
});

test('onState delivers worker state events until unsubscribed', async () => {
  const bridge = fakeBridge(() => ({ ok: true, state: {} }));
  const transport = createJevTransport(bridge);
  const seen = [];
  const unsubscribe = transport.onState((state) => seen.push(state));
  await settle();
  bridge.emit({ status: 'predicted', busy: 'auto' });
  unsubscribe();
  assert.equal(bridge.listening(), false);
  bridge.emit({ status: 'ready' });
  assert.deepEqual(seen, [{ status: 'predicted', busy: 'auto' }]);
});

class Element {
  constructor(id) {
    Object.assign(this, { id, textContent: '', innerHTML: '', hidden: false, disabled: false, value: '',
      checked: false, placeholder: '', className: '', src: '', dataset: {}, classList: { toggle() {} } });
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

test('the unchanged app.js runs on the real transport: state at boot, a stop is not an error', async () => {
  const html = readFileSync(new URL('../jev_ultrafast/static/index.html', import.meta.url), 'utf8');
  const source = readFileSync(new URL('../jev_ultrafast/static/app.js', import.meta.url), 'utf8');
  const idle = {
    busy: null, status: 'idle', goal: null, page: null, elements: [], decision: null, history: [], decisions: [],
    plan: [], plan_index: 0, elapsed_ms: 0, scenario: null, verification: null, text_model: 'text.generate',
    decision_model: 'text.decide',
    presets: { flights: { label: 'Google Flights · real web', url: 'https://www.google.com/travel/flights?hl=en', fixture: false, goal: 'Find flights.' } },
  };
  const failures = {
    predict: { code: 'stopped', message: 'Stopped. Nothing further was executed.' },
    auto: { code: 'worker_exited', message: 'The worker exited unexpectedly (exit code 3).' },
  };
  const bridge = fakeBridge(({ name }) => (failures[name] ? { ok: false, error: failures[name] } : { ok: true, state: idle }));
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new Element(id)]));
  elements.get('scenario').value = 'flights';
  elements.get('stop').hidden = true;
  const document = {
    getElementById: (id) => elements.get(id),
    querySelectorAll: () => [],
    createElement: () => new Element('link'),
  };
  const context = vm.createContext({ document, URL, console, jevTransport: createJevTransport(bridge) });
  context.window = context;
  vm.runInContext(source, context, { filename: 'app.js' });
  await settle();
  assert.equal(bridge.listening(), true);
  assert.deepEqual(bridge.invocations[0], { command: WORKER_COMMAND, payload: { name: 'state', body: {} } });
  assert.equal(elements.get('goal').value, 'Find flights.');
  assert.equal(elements.get('status').textContent, 'Ready to explore');

  bridge.emit({ ...idle, status: 'stopped' });
  assert.match(elements.get('status').textContent, /^Stopped/);

  elements.get('choose').dispatch('click');
  await settle();
  assert.deepEqual(bridge.invocations.slice(-2).map((call) => call.payload.name), ['predict', 'state']);
  assert.equal(elements.get('error').hidden, true, 'a stopped decision is not shown as an error');

  elements.get('auto').dispatch('click');
  await settle();
  assert.equal(elements.get('error').hidden, false);
  assert.equal(elements.get('error').textContent, failures.auto.message);
  assert.equal(elements.get('status').textContent, 'Paused · needs attention');
});
