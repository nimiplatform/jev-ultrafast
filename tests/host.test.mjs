// Host worker protocol (src-electron/worker-host.ts) against a scripted fake worker. No Electron, model,
// network or Chrome. Run: node --test tests/
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import * as renderer from '../renderer/transport.ts';
import {
  LineDecoder,
  WORKER_COMMAND,
  WORKER_STATE_EVENT,
  WorkerHost,
  aiErrorPayload,
  prepareScenario,
  workerEnvironment,
  workerResult,
} from '../src-electron/worker-host.ts';

const fakeWorker = fileURLToPath(new URL('./fixtures/fake-worker.mjs', import.meta.url));
const tempRoot = mkdtempSync(path.join(tmpdir(), 'jev-host-test-'));
after(() => rmSync(tempRoot, { recursive: true, force: true }));

const GOAL = '打开 Wikipedia 首页并找到今日特色条目 🚀';
const DECIDE_RESULT = {
  output: {
    type: 'text-decide',
    answers: [{
      questionId: 'operation',
      kind: 'choice',
      selectedCandidateId: 'TYPE_TEXT',
      probabilities: [{ candidateId: 'TYPE_TEXT', probability: 1 }, { candidateId: 'WAIT', probability: 0 }],
    }],
  },
  traceId: 'trace-decide',
};
const GENERATE_RESULT = {
  output: {
    type: 'text-generate',
    items: [
      { type: 'text', text: '{"text":"里斯本' },
      { type: 'reasoning-continuity', carrier: { kind: 'opaque', version: 1, payload: [1] } },
      { type: 'text', text: ' 🚀"}' },
    ],
    finishReason: 'stop',
  },
  traceId: 'trace-generate',
};
const abortedError = () => Object.assign(new Error('Local-app scenario execution was canceled by its caller.'), {
  name: 'NimiError', code: 'OPERATION_ABORTED', reasonCode: 'OPERATION_ABORTED',
});

function createHost(mode, execute) {
  const calls = [];
  const states = [];
  const logs = [];
  const host = new WorkerHost({
    launch: { executable: process.execPath, args: [fakeWorker, mode], cwd: tempRoot, missingHint: 'missing' },
    tempRoot,
    execute: execute ?? (async (spec, options) => {
      calls.push({ spec, options });
      return spec.type === 'text-decide' ? DECIDE_RESULT : GENERATE_RESULT;
    }),
    onState: (state) => states.push(state),
    log: (message) => logs.push(message),
  });
  return { host, calls, states, logs };
}

const runDirs = () => readdirSync(tempRoot).filter((name) => name.startsWith('jevu-'));

test('the renderer transport and the Host agree on the command and event names', () => {
  assert.equal(renderer.WORKER_COMMAND, WORKER_COMMAND);
  assert.equal(renderer.WORKER_STATE_EVENT, WORKER_STATE_EVENT);
});

test('lines are strict UTF-8, reassembled across chunks and bounded', () => {
  const decoder = new LineDecoder(16);
  const bytes = Buffer.from('{"a":"🚀"}\n', 'utf8');
  assert.deepEqual(decoder.push(bytes.subarray(0, 7)), []);
  assert.deepEqual(decoder.push(bytes.subarray(7)), ['{"a":"🚀"}']);
  assert.throws(() => new LineDecoder().push(Buffer.from([0x22, 0xe4, 0xb8, 0x0a])), /not valid UTF-8/);
  assert.throws(() => new LineDecoder().push(Buffer.from([0xff, 0x0a])), /not valid UTF-8/);
  assert.throws(() => new LineDecoder(16).push(Buffer.from('x'.repeat(17))), /exceeds 4 MiB/);
  const partial = new LineDecoder();
  partial.push(Buffer.from('{"a"'));
  assert.throws(() => partial.end(), /inside a line/);
});

test('the worker environment is explicit: run-scoped paths, no Host secrets or interpreter overrides', () => {
  const env = workerEnvironment(path.join(tempRoot, 'run'), {
    SystemRoot: 'C:\\Windows', LOCALAPPDATA: 'C:\\Users\\u\\AppData\\Local', 'ProgramFiles(x86)': 'C:\\PF86',
    NIMI_APP_HOST_PROFILE_DIR: 'secret-profile', OPENAI_API_KEY: 'sk-x', PYTHONPATH: 'elsewhere', PYTHONHOME: 'x',
    HTTP_PROXY: 'http://proxy', BU_CDP_URL: 'http://127.0.0.1:9222', JEV_CHROME_PATH: 'C:\\Chrome\\chrome.exe',
  });
  for (const key of ['NIMI_APP_HOST_PROFILE_DIR', 'OPENAI_API_KEY', 'PYTHONPATH', 'PYTHONHOME', 'HTTP_PROXY', 'BU_CDP_URL']) {
    assert.equal(env[key], undefined, key);
  }
  assert.equal(env.JEV_CHROME_PATH, 'C:\\Chrome\\chrome.exe');
  assert.equal(env['ProgramFiles(x86)'], 'C:\\PF86');
  assert.equal(env.JEV_BROWSER_PROFILE_DIR, path.join(tempRoot, 'run', 'profile'));
  assert.equal(env.JEV_RUNTIME_DIR, path.join(tempRoot, 'run', 'runtime'));
  assert.equal(env.TEMP, path.join(tempRoot, 'run', 'tmp'));
  assert.equal(env.PYTHONUTF8, '1');
});

test('ai_request specs map exactly onto the SDK and results flatten to what the worker validates', () => {
  const decide = { type: 'text-decide', state: { json: {} }, questions: [] };
  assert.equal(prepareScenario('decide', decide, 60000).spec, decide);
  const generate = prepareScenario('generate_text', {
    type: 'text-generate', systemPrompt: 'system', input: [{ role: 'user', content: '内容 🚀' }],
    responseFormat: { kind: 'json_schema', schemaName: 'jev_field_text', strict: true, jsonSchema: { type: 'object' } },
    maxTokens: 1024,
  }, 1000);
  assert.deepEqual(generate.spec, {
    type: 'text-generate',
    messages: [{ role: 'system', text: 'system' }, { role: 'user', text: '内容 🚀' }],
    responseFormat: { type: 'json-schema', name: 'jev_field_text', strict: true, schema: { type: 'object' } },
    maxTokens: 1024,
  });
  for (const [kind, spec, timeout] of [
    ['decide', decide, 0], ['decide', decide, 120001], ['decide', { ...decide, extra: 1 }, 1000],
    ['generate_text', { type: 'text-generate' }, 1000], ['embed', decide, 1000],
  ]) {
    assert.throws(() => prepareScenario(kind, spec, timeout));
  }
  assert.deepEqual(workerResult('decide', DECIDE_RESULT), {
    type: 'text-decide', answers: DECIDE_RESULT.output.answers, traceId: 'trace-decide',
  });
  assert.deepEqual(workerResult('generate_text', GENERATE_RESULT), {
    type: 'text-generate', text: '{"text":"里斯本 🚀"}', traceId: 'trace-generate',
  });
  assert.throws(
    () => workerResult('generate_text', { output: { ...GENERATE_RESULT.output, finishReason: 'length' }, traceId: '' }),
    /finish reason "length"/,
  );
});

test('SDK and Kit failures become the worker codes it classifies', () => {
  assert.deepEqual(aiErrorPayload(abortedError()), {
    code: 'OPERATION_ABORTED', message: 'Local-app scenario execution was canceled by its caller.',
  });
  assert.equal(aiErrorPayload(Object.assign(new Error('ai-input-limit-exceeded'), {
    name: 'NimiElectronLocalAppHostError', reasonCode: 'ai-input-limit-exceeded',
  })).code, 'AI_INPUT_LIMIT_EXCEEDED');
  assert.equal(aiErrorPayload({ reasonCode: 'ai-local-configuration-not-configured' }).code, 'AI_LOCAL_CONFIGURATION_NOT_CONFIGURED');
  assert.equal(aiErrorPayload({ code: 'SDK_LOCAL_APP_INPUT_INVALID', message: 'bad' }).code, 'SDK_LOCAL_APP_INPUT_INVALID');
  assert.equal(aiErrorPayload({ reasonCode: 'session-invalid' }).code, 'session-invalid');
  assert.equal(aiErrorPayload(new TypeError('boom')).code, 'TypeError');
  const long = aiErrorPayload({ reasonCode: 'X', message: `${'a'.repeat(3999)}🚀🚀` });
  assert.equal(Array.from(long.message).length, 4000);
  assert.equal(long.message.isWellFormed(), true);
});

test('commands and events round-trip non-ASCII text; AI runs through the SDK executor', async () => {
  const { host, calls, states } = createHost('echo');
  try {
    const started = await host.command({ name: 'start', body: { url: 'https://example.com/', goal: GOAL } });
    assert.equal(started.ok, true);
    assert.equal(started.state.goal, GOAL);
    assert.equal(states[0].busy, 'start');

    const ticked = await host.command({ name: 'tick', body: {} });
    assert.equal(ticked.ok, true, JSON.stringify(ticked));
    assert.equal(calls.length, 2);
    assert.equal(calls[0].spec.questions[0].candidates[0].description.text, '输入文字');
    assert.equal(calls[0].options.timeoutMs, 60000);
    assert.equal(calls[0].options.signal instanceof AbortSignal, true);
    assert.deepEqual(calls[1].spec.messages[0], { role: 'system', text: 'Return {"text": value}.' });
    assert.equal(JSON.parse(calls[1].spec.messages[1].text).goal, '输入目的地 🚀');
    assert.deepEqual(calls[1].spec.responseFormat.type, 'json-schema');
    const [decide, generate] = ticked.state.received;
    assert.deepEqual(decide, { ok: true, result: { type: 'text-decide', answers: DECIDE_RESULT.output.answers, traceId: 'trace-decide' } });
    assert.deepEqual(generate, { ok: true, result: { type: 'text-generate', text: '{"text":"里斯本 🚀"}', traceId: 'trace-generate' } });
    assert.equal(ticked.state.history[0].text, '里斯本 🚀');
  } finally {
    await host.shutdown();
  }
  assert.deepEqual(runDirs(), []);
});

test('a stop aborts the in-flight AI request; the worker reports the stop', async () => {
  const { host } = createHost('echo', (spec, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(abortedError()), { once: true });
  }));
  try {
    await host.command({ name: 'start', body: { url: 'https://example.com/', goal: GOAL } });
    const predict = host.command({ name: 'predict', body: {} });
    await new Promise((resolve) => setTimeout(resolve, 200));
    const stopped = await host.command({ name: 'stop', body: {} });
    assert.equal(stopped.ok, true);
    assert.deepEqual(await predict, { ok: false, error: { code: 'stopped', message: 'Stopped. Nothing further was executed.' } });
    await new Promise((resolve) => setTimeout(resolve, 100));
    const { state } = await host.command({ name: 'state', body: {} });
    assert.equal(state.status, 'stopped');
    assert.deepEqual(state.received, [{ late: { ok: false, error: {
      code: 'OPERATION_ABORTED', message: 'Local-app scenario execution was canceled by its caller.',
    } } }]);
  } finally {
    await host.shutdown();
  }
});

test('an invalid ai_request is answered with AI_INPUT_INVALID and never executed', async () => {
  const { host, calls } = createHost('bad-ai-request');
  try {
    const reply = await host.command({ name: 'tick', body: {} });
    assert.equal(calls.length, 0);
    assert.equal(reply.state.received[0].ok, false);
    assert.equal(reply.state.received[0].error.code, 'AI_INPUT_INVALID');
  } finally {
    await host.shutdown();
  }
});

for (const mode of ['invalid-utf8', 'truncated-utf8', 'oversized', 'not-json', 'unknown-response']) {
  test(`a protocol violation (${mode}) stops the worker; only a start launches a new one`, async () => {
    const { host, states } = createHost(mode);
    try {
      const started = await host.command({ name: 'start', body: { url: 'https://example.com/', goal: GOAL } });
      assert.equal(started.ok, true);
      const reply = await host.command({ name: 'tick', body: {} });
      assert.equal(reply.ok, false);
      assert.equal(reply.error.code, 'worker_protocol_error');
      assert.equal(host.running, false);
      const view = await host.command({ name: 'state', body: {} });
      assert.equal(view.ok, true);
      assert.equal(view.state.busy, null);
      assert.equal(view.state.status, 'blocked');
      assert.match(view.state.blocked_reason, /broke the protocol/);
      assert.equal(states.at(-1).status, 'blocked');
      assert.equal((await host.command({ name: 'predict', body: {} })).error.code, 'worker_protocol_error');
      assert.equal(host.running, false, 'nothing but start/preset restarts the worker');
      const restarted = await host.command({ name: 'preset', body: { scenario: 'travel', goal: GOAL } });
      assert.equal(restarted.ok, true);
      assert.equal(host.running, true);
    } finally {
      await host.shutdown();
    }
  });
}

test('an unexpected exit fails the pending command and unlocks the inspector', async () => {
  const { host, states } = createHost('crash');
  try {
    await host.command({ name: 'start', body: { url: 'https://example.com/', goal: GOAL } });
    const reply = await host.command({ name: 'act', body: { fingerprint: 'a'.repeat(64) } });
    assert.equal(reply.ok, false);
    assert.equal(reply.error.code, 'worker_exited');
    assert.match(reply.error.message, /exit code 3/);
    assert.equal(states.at(-1).busy, null);
    assert.equal(states.at(-1).goal, GOAL);
    assert.equal((await host.command({ name: 'stop', body: {} })).error.code, 'worker_exited');
  } finally {
    await host.shutdown();
  }
});

test('invalid renderer commands are refused without starting a worker', async () => {
  const { host } = createHost('echo');
  for (const payload of [
    { name: 'shell', body: {} },
    { name: 'state', body: {}, extra: true },
    { name: 'start', body: [] },
    { name: 'start', body: { goal: 'x'.repeat(70 * 1024) } },
    { name: 'start', body: { when: new Date() } },
    // The worker would drop this line without a reply, so it never reaches it.
    { name: 'start', body: { goal: `stop ${String.fromCharCode(0xd800)} here` } },
    'state',
  ]) {
    const reply = await host.command(payload);
    assert.equal(reply.ok, false);
    assert.equal(reply.error.code, 'invalid_command');
  }
  assert.equal(host.running, false);
  assert.equal((await host.command({ name: 'stop', body: {} })).error.code, 'worker_not_running');
  assert.deepEqual(runDirs(), []);
});

test('a missing worker executable is reported, not retried silently', async () => {
  const host = new WorkerHost({
    launch: { executable: path.join(tempRoot, 'missing.exe'), args: [], cwd: tempRoot, missingHint: 'Build the worker.' },
    tempRoot,
    execute: async () => { throw new Error('unused'); },
  });
  const reply = await host.command({ name: 'state', body: {} });
  assert.equal(reply.ok, false);
  assert.equal(reply.error.code, 'worker_unavailable');
  assert.match(reply.error.message, /Build the worker/);
});
