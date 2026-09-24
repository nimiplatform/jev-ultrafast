// PACKAGING / CONNECTION CHECK — scripted AI answers, not a model result.
//
// Starts the PACKAGED worker (dist-electron-package/.../resources/jev-worker/jev-worker.exe) through the
// Host's own worker module (src-electron/worker-host.ts) with a scripted executor in place of Nimi: every
// text.decide question gets a fixed choice (TYPE_TEXT when offered, else WAIT) and text.generate returns a
// fixed Chinese value. It checks the frozen build (fixture server, snapshot.js, Browser Harness daemon),
// strict UTF-8 both ways (a Chinese + emoji goal round trip; the typed text read back from the page), a
// stop that aborts a pending decision, close, and that the automation Chrome, the daemon and the run
// directory are gone while the user's own Chrome processes are untouched. Windows only.
//
// `--source` runs the same journey against the development worker the Host starts under `pnpm dev`
// (the project .venv Python, `-m jev_ultrafast.worker`), with the fixture page from the source tree.
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { WorkerHost, resolveWorkerLaunch } from '../src-electron/worker-host.ts';

const GOAL = '在目的地输入框中输入里斯本，然后停下 🚀 (packaging check)';
const TYPED = '里斯本 🚀 Lisboa';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
if (process.platform !== 'win32') throw new Error('This check inspects Windows processes; run it on Windows.');
const source = process.argv.includes('--source');
const resources = path.join(root, 'dist-electron-package', 'jev-ultrafast-shell-win32-x64', 'resources');
const launch = source
  ? resolveWorkerLaunch({ packaged: false, appRoot: root })
  : resolveWorkerLaunch({ packaged: true, resourcesPath: resources });
if (!existsSync(launch.executable)) {
  throw new Error(source
    ? `Create the project .venv first (uv sync): ${launch.executable}`
    : `Build the production package first (pnpm run build:electron:production): ${launch.executable}`);
}
const fixturePage = source
  ? path.join(root, 'jev_ultrafast', 'static', 'fixture.html')
  : path.join(resources, 'jev-worker', '_internal', 'jev_ultrafast', 'static', 'fixture.html');

function processes(name) {
  const script = '[Console]::OutputEncoding=[Text.Encoding]::UTF8;'
    + `Get-CimInstance Win32_Process -Filter "Name='${name}'" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress`;
  const output = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    encoding: 'utf8', windowsHide: true, timeout: 60_000,
  }).trim();
  const rows = output ? JSON.parse(output) : [];
  return (Array.isArray(rows) ? rows : [rows]).map((row) => ({
    pid: row.ProcessId, exe: row.ExecutablePath || '', line: row.CommandLine || '',
  }));
}
const samePath = (a, b) => path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase();
// Packaged: the worker and its daemon are both jev-worker.exe. Source: .venv Python (a venv launcher and its
// interpreter) running the worker module or the daemon module.
const workerProcesses = () => (source
  ? processes('python.exe').filter((p) => ['jev_ultrafast.worker', 'browser_harness.daemon'].some((m) => p.line.includes(m)))
  : processes('jev-worker.exe').filter((p) => p.exe && samePath(p.exe, launch.executable)));
const workersBefore = new Set(workerProcesses().map((p) => p.pid));
const ours = (tempRoot) => ({
  chrome: processes('chrome.exe').filter((p) => p.line.toLowerCase().includes(tempRoot.toLowerCase())),
  workers: workerProcesses().filter((p) => !workersBefore.has(p.pid)),
});
const until = async (predicate, timeoutMs) => {
  const deadline = Date.now() + timeoutMs;
  while (!(await predicate())) {
    if (Date.now() > deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return true;
};
const elapsed = async (run) => {
  const started = performance.now();
  const value = await run();
  return [value, Math.round(performance.now() - started)];
};

// Loopback server for the packaged fixture page (the `start` command needs an http(s) start URL).
const page = readFileSync(fixturePage);
const server = createServer((request, response) => {
  const ok = request.method === 'GET' && new URL(request.url, 'http://127.0.0.1').pathname === '/fixture.html';
  response.writeHead(ok ? 200 : 404, { 'Content-Type': ok ? 'text/html; charset=utf-8' : 'text/plain' });
  response.end(ok ? page : 'Not found');
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const startUrl = `http://127.0.0.1:${server.address().port}/fixture.html?scenario=travel`;

const tempRoot = mkdtempSync(path.join(tmpdir(), 'jev-packaged-check-'));
const requests = [];
const logs = [];
let holdDecisions = false;
const aborted = () => Object.assign(new Error('Local-app scenario execution was canceled by its caller.'), {
  name: 'NimiError', code: 'OPERATION_ABORTED', reasonCode: 'OPERATION_ABORTED',
});
// The scripted stand-in for bridge.services.ai.scenario.execute: fixed answers in the SDK result shape.
function execute(spec, { signal, timeoutMs }) {
  requests.push({ spec, timeoutMs });
  if (holdDecisions) {
    return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(aborted()), { once: true }));
  }
  if (spec.type === 'text-decide') {
    const question = spec.questions[0];
    const ids = question.candidates.map((candidate) => candidate.id);
    const selected = question.id !== 'operation' ? ids[0] : ids.includes('TYPE_TEXT') ? 'TYPE_TEXT' : 'WAIT';
    return Promise.resolve({
      output: {
        type: 'text-decide',
        answers: [{
          questionId: question.id,
          kind: 'choice',
          selectedCandidateId: selected,
          probabilities: ids.map((id) => ({ candidateId: id, probability: id === selected ? 1 : 0 })),
        }],
      },
      traceId: 'packaged-check-fixed-choice',
    });
  }
  return Promise.resolve({
    output: { type: 'text-generate', items: [{ type: 'text', text: JSON.stringify({ text: TYPED }) }], finishReason: 'stop' },
    traceId: 'packaged-check-fixed-text',
  });
}

const checks = {};
const timings = {};
const userChromeBefore = processes('chrome.exe').filter((p) => !p.line.toLowerCase().includes(tempRoot.toLowerCase()));
const host = new WorkerHost({ launch, tempRoot, execute, log: (line) => logs.push(line) });
let failure = null;
try {
  const expect = (reply, name) => {
    if (!reply.ok) throw new Error(`${name} failed: ${reply.error.code}: ${reply.error.message}`);
    return reply.state;
  };
  let state = expect(await host.command({ name: 'state', body: {} }), 'state');
  checks.idle_state = state.status === 'idle' && Boolean(state.presets?.flights?.goal);

  let ms;
  [state, ms] = await elapsed(async () => expect(await host.command({ name: 'preset', body: { scenario: 'travel', goal: GOAL } }), 'preset'));
  timings.preset_ms = ms;
  checks.worker_fixture_server = state.page?.url?.startsWith('http://127.0.0.1:') && state.page?.title === 'Forma · Find a place to slow down';
  checks.preset_goal_round_trip = state.goal === GOAL;
  checks.observation_indexed = (state.elements?.length ?? 0) >= 5 && (state.page?.screenshot?.length ?? 0) > 1000;
  const running = ours(tempRoot);
  checks.dedicated_chrome_running = running.chrome.length > 0;
  checks.worker_and_daemon_running = running.workers.length >= 2
    && running.workers.some((p) => p.line.includes('browser_harness.daemon'));

  [state, ms] = await elapsed(async () => expect(await host.command({ name: 'start', body: { url: startUrl, goal: GOAL } }), 'start'));
  timings.start_ms = ms;
  checks.start_on_local_fixture = state.page?.url === startUrl && state.status === 'ready';
  checks.start_goal_round_trip = state.goal === GOAL;

  [state, ms] = await elapsed(async () => expect(await host.command({ name: 'tick', body: {} }), 'tick'));
  timings.tick_ms = ms;
  const [decide, generate] = requests.slice(-2);
  checks.one_decide_then_generate = decide?.spec.type === 'text-decide' && generate?.spec.type === 'text-generate';
  // Worker -> Host: the goal inside the worker's own requests decodes exactly.
  checks.worker_output_utf8 = decide?.spec.questions?.[0]?.instructions?.json?.goal === GOAL
    && JSON.parse(generate?.spec.messages?.[1]?.text ?? '{}').goal === GOAL;
  // Host -> worker -> Chrome -> worker -> Host: the generated text was typed and is read back from the page.
  const step = state.history?.[0];
  checks.typed_text_round_trip = step?.kind === 'fill' && step?.text === TYPED && step?.decision_calls === 1;
  checks.page_value_round_trip = (state.elements ?? []).some((element) => element.value === TYPED);

  holdDecisions = true;
  const predict = host.command({ name: 'predict', body: {} });
  checks.decision_requested = await until(() => requests.length > 2, 30_000);
  const stopped = await host.command({ name: 'stop', body: {} });
  const predicted = await predict;
  checks.stop_answered = stopped.ok;
  checks.pending_decision_stopped = !predicted.ok && predicted.error.code === 'stopped';
  state = expect(await host.command({ name: 'state', body: {} }), 'state');
  checks.nothing_executed_after_stop = state.status === 'stopped' && state.decision === null && state.history.length === 1;

  const closed = await host.command({ name: 'close', body: {} });
  checks.closed = closed.ok && closed.state.status === 'idle';
  checks.worker_exited = await until(() => !host.running, 30_000);
  await host.shutdown();
  checks.chrome_and_daemon_gone = await until(() => {
    const left = ours(tempRoot);
    return left.chrome.length === 0 && left.workers.length === 0;
  }, 20_000);
  checks.run_directory_removed = readdirSync(tempRoot).length === 0;
  const chromeAfter = new Set(processes('chrome.exe').map((p) => p.pid));
  checks.user_chrome_untouched = userChromeBefore.every((p) => chromeAfter.has(p.pid));
} catch (error) {
  failure = error instanceof Error ? error.stack || error.message : String(error);
} finally {
  await host.shutdown().catch(() => undefined);
  await new Promise((resolve) => server.close(resolve));
  rmSync(tempRoot, { recursive: true, force: true });
}

const passed = !failure && Object.values(checks).every(Boolean);
const report = {
  kind: 'PACKAGING/CONNECTION CHECK - scripted AI answers through the Host worker module, not a model result',
  worker: source ? `${launch.executable} ${launch.args.join(' ')} (source)` : launch.executable,
  passed,
  checks,
  timings,
  user_chrome_processes_observed: userChromeBefore.length,
  ...(failure ? { failure } : {}),
  host_log_tail: logs.slice(-12),
};
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (!passed) process.exitCode = 1;
