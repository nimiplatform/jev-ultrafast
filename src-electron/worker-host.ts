// The Host side of the Jev worker protocol (jev_ultrafast/worker.py, README "Worker protocol").
//
// One worker process at a time speaks newline-delimited JSON over its stdin/stdout in strict UTF-8.
// The Host forwards the renderer's fixed commands, executes the worker's bounded ai_request messages
// with the Nimi SDK AI client (text.decide / text.generate) and answers with ai_result. Framing or
// protocol violations fail closed: the worker is terminated and pending commands fail. A crashed
// worker is restarted only by an explicit start/preset; no business step is replayed.
//
// Pure Node (no Electron import): the tests and the packaged-worker check drive this exact module.
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdirSync, readdirSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import path from 'node:path';
import type {
  NimiLocalAppScenarioExecuteResult,
  NimiLocalAppScenarioExecuteSpec,
} from '@nimiplatform/sdk/app';

/** The one App command the renderer may invoke; its payload is {name, body}. */
export const WORKER_COMMAND = 'jev.worker.command';
/** The event carrying the worker's state messages to the renderer. */
export const WORKER_STATE_EVENT = 'jev.worker.state';
export const WORKER_COMMAND_NAMES = Object.freeze([
  'start', 'preset', 'predict', 'act', 'tick', 'auto', 'stop', 'state', 'close',
] as const);
export type WorkerCommandName = (typeof WORKER_COMMAND_NAMES)[number];

/** Bound of one protocol line in bytes, excluding its newline (the worker's own bound). */
export const MAX_LINE_BYTES = 4 * 1024 * 1024;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_JSON_DEPTH = 16;
const MAX_ERROR_CODE_CHARS = 128;
const MAX_ERROR_MESSAGE_CHARS = 4000;
const MAX_SCENARIO_TIMEOUT_MS = 120_000;
const MAX_TEXT_INPUT_MESSAGES = 64;
const STDERR_TAIL_CHARS = 8000;
const CLOSE_TIMEOUT_MS = 30_000;
const KILL_WAIT_MS = 5_000;
const RUN_DIR_PATTERN = /^jevu-(\d+)-[0-9a-f]{6}$/u;

export type JsonObject = { [key: string]: unknown };
export type WorkerError = { code: string; message: string };
export type WorkerReply = { ok: true; state: JsonObject } | { ok: false; error: WorkerError };
export type WorkerLaunch = {
  readonly executable: string;
  readonly args: readonly string[];
  readonly cwd: string;
  /** Shown when the executable does not exist. */
  readonly missingHint: string;
};
/** bridge.services.ai.scenario.execute from the Kit Electron App bridge (the SDK AI client). */
export type ScenarioExecute = (
  spec: NimiLocalAppScenarioExecuteSpec,
  options: { signal: AbortSignal; timeoutMs: number },
) => Promise<NimiLocalAppScenarioExecuteResult>;
export type WorkerHostOptions = {
  readonly launch: WorkerLaunch;
  /** Parent of the per-run directories, e.g. Electron's app.getPath('temp') bound to the Host profile. */
  readonly tempRoot: string;
  readonly execute: ScenarioExecute;
  readonly onState?: (state: JsonObject) => void;
  readonly log?: (message: string) => void;
  /** Source of the passthrough variables; the worker never inherits the Host environment wholesale. */
  readonly environment?: NodeJS.ProcessEnv;
};

// ----- launch and environment ------------------------------------------------------------------------

/**
 * Where the worker runs. Development: the project's .venv interpreter runs `-m jev_ultrafast.worker`
 * from the App root. Packaged: the PyInstaller onedir `jev-worker` shipped in the App resources.
 */
export function resolveWorkerLaunch(
  input: { readonly packaged: true; readonly resourcesPath: string } | { readonly packaged: false; readonly appRoot: string },
): WorkerLaunch {
  const windows = process.platform === 'win32';
  if (input.packaged) {
    const directory = path.join(input.resourcesPath, 'jev-worker');
    return {
      executable: path.join(directory, windows ? 'jev-worker.exe' : 'jev-worker'),
      args: [],
      cwd: directory,
      missingHint: 'The packaged worker is missing from the App resources; reinstall the App.',
    };
  }
  return {
    executable: path.join(input.appRoot, '.venv', windows ? 'Scripts' : 'bin', windows ? 'python.exe' : 'python'),
    args: ['-m', 'jev_ultrafast.worker'],
    cwd: input.appRoot,
    missingHint: 'The project virtual environment is missing; create it with `uv sync` (see README).',
  };
}

const INHERITED_ENVIRONMENT = [
  'SystemRoot', 'SystemDrive', 'WINDIR', 'ComSpec', 'PATHEXT', 'OS',
  'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME',
  'LOCALAPPDATA', 'APPDATA', 'ProgramData', 'ALLUSERSPROFILE', 'PUBLIC',
  'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432',
  'CommonProgramFiles', 'CommonProgramFiles(x86)', 'CommonProgramW6432',
  'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER',
  'HOME', 'LANG', 'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'XDG_CONFIG_HOME',
] as const;

/**
 * The worker's explicit environment: OS basics the interpreter and Chrome need, no Nimi Host variables,
 * credentials or proxies. Everything the worker and its Chrome write goes under `runDir`, which the Host
 * removes after the worker exits: the automation profile, the worker's runtime files and TEMP.
 */
export function workerEnvironment(runDir: string, source: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const env: Record<string, string> = {};
  for (const key of INHERITED_ENVIRONMENT) {
    const value = source[key];
    if (typeof value === 'string' && value) env[key] = value;
  }
  if (process.platform === 'win32') {
    const systemRoot = env.SystemRoot || env.WINDIR || 'C:\\Windows';
    const system32 = path.join(systemRoot, 'System32');
    // Browser Harness shells out to tasklist and powershell; nothing else is looked up on PATH.
    env.PATH = [system32, systemRoot, path.join(system32, 'Wbem'), path.join(system32, 'WindowsPowerShell', 'v1.0')].join(';');
  } else {
    env.PATH = '/usr/local/bin:/usr/bin:/bin';
  }
  const temp = path.join(runDir, 'tmp');
  env.TEMP = temp;
  env.TMP = temp;
  env.TMPDIR = temp;
  env.JEV_BROWSER_PROFILE_DIR = path.join(runDir, 'profile');
  env.JEV_RUNTIME_DIR = path.join(runDir, 'runtime');
  if (source.JEV_CHROME_PATH) env.JEV_CHROME_PATH = source.JEV_CHROME_PATH;
  if (source.JEV_CHROME_HEADLESS === '1') env.JEV_CHROME_HEADLESS = '1';
  // The worker reconfigures its protocol streams to strict UTF-8 itself; these only keep the
  // interpreter's other defaults (open(), user site-packages, bytecode files) predictable.
  env.PYTHONUTF8 = '1';
  env.PYTHONNOUSERSITE = '1';
  env.PYTHONDONTWRITEBYTECODE = '1';
  return env;
}

function createRunDir(tempRoot: string): string {
  mkdirSync(tempRoot, { recursive: true });
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const directory = path.join(tempRoot, `jevu-${process.pid}-${randomBytes(3).toString('hex')}`);
    try {
      mkdirSync(directory);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'EEXIST') continue;
      throw error;
    }
    mkdirSync(path.join(directory, 'tmp'));
    return directory;
  }
  throw new Error('Could not create a worker run directory.');
}

async function removeRunDir(directory: string, log?: (message: string) => void): Promise<void> {
  try {
    // Chrome can release profile files shortly after its process tree has ended.
    await rm(directory, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  } catch (error) {
    log?.(`could not remove the worker run directory ${directory}: ${describeError(error)}`);
  }
}

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === 'EPERM';
  }
}

/** Remove run directories left by an earlier Host that ended without cleaning up (for example, killed). */
export async function sweepStaleRunDirs(tempRoot: string, log?: (message: string) => void): Promise<void> {
  let entries;
  try {
    entries = readdirSync(tempRoot, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    const match = entry.isDirectory() ? RUN_DIR_PATTERN.exec(entry.name) : null;
    if (!match) continue;
    const pid = Number(match[1]);
    if (pid === process.pid || processAlive(pid)) continue;
    await removeRunDir(path.join(tempRoot, entry.name), log);
  }
}

/** End the worker and everything it started (its Chrome, the harness daemon, a venv launcher's child). */
function terminateProcessTree(child: ChildProcessWithoutNullStreams): void {
  if (child.exitCode !== null || child.signalCode !== null || child.pid === undefined) return;
  if (process.platform === 'win32') {
    const systemRoot = process.env.SystemRoot || 'C:\\Windows';
    const killer = spawn(path.join(systemRoot, 'System32', 'taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    killer.once('error', () => child.kill());
    killer.once('exit', (code) => {
      if (code !== 0) child.kill();
    });
    return;
  }
  child.kill('SIGKILL');
}

// ----- JSON and text helpers ------------------------------------------------------------------------

function isPlainObject(value: unknown): value is JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isJsonValue(value: unknown, depth = 0): boolean {
  if (depth > MAX_JSON_DEPTH) return false;
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return true;
  if (typeof value === 'number') return Number.isFinite(value);
  if (Array.isArray(value)) return value.every((item) => isJsonValue(item, depth + 1));
  if (isPlainObject(value)) return Object.values(value).every((item) => isJsonValue(item, depth + 1));
  return false;
}

function hasExactKeys(value: JsonObject, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(value, key));
}

/** At most `max` code points, never splitting a surrogate pair, never containing a lone surrogate. */
function boundedText(value: string, max: number): string {
  const wellFormed = value.toWellFormed();
  return wellFormed.length <= max ? wellFormed : Array.from(wellFormed).slice(0, max).join('');
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === 'string' ? error : 'unknown error';
}

function failure(code: string, message: string): WorkerReply {
  return { ok: false, error: { code, message } };
}

/** A renderer command the Host refuses before it reaches the worker. */
class CommandRejected extends Error {}
/** The worker broke the protocol; the Host stops it. */
class ProtocolViolation extends Error {}
/** A Host-side AI failure with its own code (the worker treats unknown codes as ai_error). */
class HostAIError extends Error {
  readonly reasonCode: string;
  constructor(reasonCode: string, message: string) {
    super(message);
    this.reasonCode = reasonCode;
  }
}

// ----- renderer command payload ---------------------------------------------------------------------

const LONE_SURROGATE = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

function wellFormedText(value: unknown): boolean {
  if (typeof value === 'string') return !LONE_SURROGATE.test(value);
  if (Array.isArray(value)) return value.every(wellFormedText);
  if (isPlainObject(value)) return Object.entries(value).every(([key, entry]) => wellFormedText(key) && wellFormedText(entry));
  return true;
}

/** Validate the renderer's {name, body}: a fixed worker command name and a small plain JSON body. */
export function parseCommandPayload(payload: unknown): { name: WorkerCommandName; body: JsonObject } {
  if (!isPlainObject(payload) || !hasExactKeys(payload, Object.hasOwn(payload, 'body') ? ['name', 'body'] : ['name'])) {
    throw new CommandRejected('A worker command is {name, body}.');
  }
  const name = payload.name;
  if (typeof name !== 'string' || !(WORKER_COMMAND_NAMES as readonly string[]).includes(name)) {
    throw new CommandRejected(`Unknown worker command ${JSON.stringify(name)}.`);
  }
  const body = payload.body === undefined ? {} : payload.body;
  if (!isPlainObject(body) || !isJsonValue(body)) throw new CommandRejected('A command body is a plain JSON object.');
  // The worker drops a line that is not strict UTF-8 without replying, so refuse it here instead.
  if (!wellFormedText(body)) throw new CommandRejected('Command text contains an unpaired surrogate.');
  if (Buffer.byteLength(JSON.stringify(body), 'utf8') > MAX_BODY_BYTES) {
    throw new CommandRejected('A command body is at most 64 KiB.');
  }
  return { name: name as WorkerCommandName, body };
}

// ----- framing --------------------------------------------------------------------------------------

/**
 * Newline-delimited strict UTF-8. A streaming TextDecoder in fatal mode carries characters split across
 * chunks and is flushed at every newline, so each line must end on a complete character. A line over
 * the byte bound, invalid UTF-8 or output that ends inside a line throws.
 */
export class LineDecoder {
  readonly #maxLineBytes: number;
  readonly #decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  #text = '';
  #bytes = 0;

  constructor(maxLineBytes = MAX_LINE_BYTES) {
    this.#maxLineBytes = maxLineBytes;
  }

  push(chunk: Uint8Array): string[] {
    const lines: string[] = [];
    let start = 0;
    for (;;) {
      const newline = chunk.indexOf(0x0a, start);
      const end = newline === -1 ? chunk.length : newline;
      if (end > start) {
        this.#bytes += end - start;
        if (this.#bytes > this.#maxLineBytes) throw new ProtocolViolation('a line exceeds 4 MiB');
        this.#text += this.#decode(chunk.subarray(start, end), true);
      }
      if (newline === -1) return lines;
      this.#text += this.#decode(new Uint8Array(0), false);
      lines.push(this.#text);
      this.#text = '';
      this.#bytes = 0;
      start = newline + 1;
    }
  }

  /** The stream ended; a partial line is a violation. */
  end(): void {
    if (this.#bytes > 0) throw new ProtocolViolation('the output ended inside a line');
  }

  #decode(bytes: Uint8Array, stream: boolean): string {
    try {
      return this.#decoder.decode(bytes, { stream });
    } catch {
      throw new ProtocolViolation('the output is not valid UTF-8');
    }
  }
}

type WorkerMessage =
  | { type: 'response'; id: number; reply: WorkerReply }
  | { type: 'event'; state: JsonObject }
  | { type: 'ai_request'; id: number; kind: unknown; spec: unknown; timeoutMs: unknown };

function validId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

/** One worker line -> message. Anything outside the closed worker -> Host message set is a violation. */
export function parseWorkerMessage(line: string): WorkerMessage {
  let message: unknown;
  try {
    message = JSON.parse(line);
  } catch {
    throw new ProtocolViolation('a line is not JSON');
  }
  if (!isPlainObject(message)) throw new ProtocolViolation('a message is not a JSON object');
  if (message.type === 'response' && validId(message.id)) {
    if (message.ok === true && hasExactKeys(message, ['type', 'id', 'ok', 'state']) && isPlainObject(message.state)) {
      return { type: 'response', id: message.id, reply: { ok: true, state: message.state } };
    }
    const error = message.error;
    if (
      message.ok === false
      && hasExactKeys(message, ['type', 'id', 'ok', 'error'])
      && isPlainObject(error)
      && hasExactKeys(error, ['code', 'message'])
      && typeof error.code === 'string'
      && error.code.length > 0
      && typeof error.message === 'string'
    ) {
      return { type: 'response', id: message.id, reply: failure(error.code, error.message) };
    }
  }
  if (message.type === 'event' && hasExactKeys(message, ['type', 'name', 'state']) && message.name === 'state'
    && isPlainObject(message.state)) {
    return { type: 'event', state: message.state };
  }
  if (message.type === 'ai_request' && hasExactKeys(message, ['type', 'id', 'kind', 'spec', 'timeoutMs'])
    && validId(message.id)) {
    return { type: 'ai_request', id: message.id, kind: message.kind, spec: message.spec, timeoutMs: message.timeoutMs };
  }
  throw new ProtocolViolation(`an unexpected ${JSON.stringify(message.type)} message`);
}

// ----- AI requests ----------------------------------------------------------------------------------

type PreparedScenario = {
  kind: 'decide' | 'generate_text';
  spec: NimiLocalAppScenarioExecuteSpec;
  timeoutMs: number;
};
type TextGenerateSpec = Extract<NimiLocalAppScenarioExecuteSpec, { readonly type: 'text-generate' }>;
type ResponseSchema = NonNullable<NonNullable<TextGenerateSpec['responseFormat']>['schema']>;

/**
 * The worker's ai_request -> the SDK execute spec. `decide` already is the SDK text-decide spec (the SDK
 * validates it in full). `generate_text` maps {systemPrompt, input, responseFormat, maxTokens} onto the
 * SDK text-generate turn: a system message, the user messages, and a strict json-schema response format.
 */
export function prepareScenario(kind: unknown, spec: unknown, timeoutMs: unknown): PreparedScenario {
  if (typeof timeoutMs !== 'number' || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > MAX_SCENARIO_TIMEOUT_MS) {
    throw new Error('timeoutMs must be an integer from 1 to 120000');
  }
  if (!isPlainObject(spec)) throw new Error('spec must be an object');
  if (kind === 'decide') {
    if (!hasExactKeys(spec, ['type', 'state', 'questions']) || spec.type !== 'text-decide' || !Array.isArray(spec.questions)) {
      throw new Error('a decide spec is {type:"text-decide", state, questions}');
    }
    return { kind, timeoutMs, spec: spec as unknown as NimiLocalAppScenarioExecuteSpec };
  }
  if (kind !== 'generate_text') throw new Error(`unknown ai_request kind ${JSON.stringify(kind)}`);
  if (!hasExactKeys(spec, ['type', 'systemPrompt', 'input', 'responseFormat', 'maxTokens']) || spec.type !== 'text-generate') {
    throw new Error('a generate_text spec is {type:"text-generate", systemPrompt, input, responseFormat, maxTokens}');
  }
  const { systemPrompt, input, responseFormat, maxTokens } = spec;
  if (typeof systemPrompt !== 'string' || !systemPrompt.trim()) throw new Error('systemPrompt must be text');
  if (!Array.isArray(input) || input.length === 0 || input.length > MAX_TEXT_INPUT_MESSAGES) {
    throw new Error('input must hold 1-64 user messages');
  }
  const messages = input.map((item) => {
    if (!isPlainObject(item) || !hasExactKeys(item, ['role', 'content']) || item.role !== 'user'
      || typeof item.content !== 'string' || !item.content.trim()) {
      throw new Error('each input message is {role:"user", content}');
    }
    return { role: 'user' as const, text: item.content };
  });
  if (
    !isPlainObject(responseFormat)
    || !hasExactKeys(responseFormat, ['kind', 'schemaName', 'strict', 'jsonSchema'])
    || responseFormat.kind !== 'json_schema'
    || typeof responseFormat.schemaName !== 'string'
    || !responseFormat.schemaName
    || typeof responseFormat.strict !== 'boolean'
    || !isPlainObject(responseFormat.jsonSchema)
  ) {
    throw new Error('responseFormat must be {kind:"json_schema", schemaName, strict, jsonSchema}');
  }
  if (typeof maxTokens !== 'number' || !Number.isSafeInteger(maxTokens) || maxTokens < 1) {
    throw new Error('maxTokens must be a positive integer');
  }
  const generate: TextGenerateSpec = {
    type: 'text-generate',
    messages: [{ role: 'system', text: systemPrompt }, ...messages],
    responseFormat: {
      type: 'json-schema',
      name: responseFormat.schemaName,
      strict: responseFormat.strict,
      // A plain JSON object from the worker's strict JSON; the SDK validates every value again.
      schema: responseFormat.jsonSchema as ResponseSchema,
    },
    maxTokens,
  };
  return { kind, timeoutMs, spec: generate };
}

/**
 * The SDK result -> the worker's ai_result result. text.decide: {output:{type,answers},traceId} flattens
 * to {type,answers,traceId}. text.generate: the text items of a completed (finishReason "stop") output
 * joined in order, {type:"text-generate", text, traceId}; the worker validates the JSON itself.
 */
export function workerResult(kind: PreparedScenario['kind'], result: unknown): JsonObject {
  if (!isPlainObject(result) || !isPlainObject(result.output)) {
    throw new HostAIError('host_result_invalid', 'The AI result has no output.');
  }
  const output = result.output;
  const trace = typeof result.traceId === 'string' && result.traceId ? { traceId: result.traceId } : {};
  if (kind === 'decide') {
    if (output.type !== 'text-decide' || !Array.isArray(output.answers)) {
      throw new HostAIError('host_result_invalid', 'The text.decide result has no answers.');
    }
    return { type: 'text-decide', answers: output.answers, ...trace };
  }
  if (output.type !== 'text-generate' || !Array.isArray(output.items)) {
    throw new HostAIError('host_result_invalid', 'The text.generate result has no output items.');
  }
  if (output.finishReason !== 'stop') {
    throw new HostAIError(
      'text_generate_incomplete',
      `text.generate ended with finish reason ${JSON.stringify(output.finishReason)}; no field text is used.`,
    );
  }
  const text = output.items
    .filter((item): item is { type: 'text'; text: string } => isPlainObject(item) && item.type === 'text' && typeof item.text === 'string')
    .map((item) => item.text)
    .join('');
  return { type: 'text-generate', text, ...trace };
}

/**
 * {code, message} for a failed AI request: code = reasonCode ?? code ?? name. Kit's local-app Host spells
 * Runtime AI reason codes in kebab case (AI_INPUT_LIMIT_EXCEEDED -> ai-input-limit-exceeded); those are
 * restored to the Runtime spelling the worker classifies. Every code fails closed in the worker.
 */
export function aiErrorPayload(error: unknown): WorkerError {
  const record = error && typeof error === 'object' ? (error as Record<string, unknown>) : {};
  const raw = [record.reasonCode, record.code, record.name].find(
    (value): value is string => typeof value === 'string' && value.trim() !== '',
  );
  let code = (raw ?? 'HOST_AI_ERROR').trim();
  if (/^ai(?:-[a-z0-9]+)+$/u.test(code)) code = code.toUpperCase().replaceAll('-', '_');
  const message = typeof record.message === 'string' ? record.message : typeof error === 'string' ? error : '';
  return { code: boundedText(code, MAX_ERROR_CODE_CHARS), message: boundedText(message, MAX_ERROR_MESSAGE_CHARS) };
}

// ----- the worker process ---------------------------------------------------------------------------

type WorkerProcess = {
  readonly child: ChildProcessWithoutNullStreams;
  readonly runDir: string;
  readonly pending: Map<number, (reply: WorkerReply) => void>;
  readonly ai: Map<number, AbortController>;
  readonly done: Promise<void>;
  nextCommandId: number;
  expectedExit: boolean;
  exited: boolean;
  violation: string | null;
  spawnError: string | null;
  stderrTail: string;
  finish: () => void;
};

export class WorkerHost {
  readonly #options: WorkerHostOptions;
  #worker: WorkerProcess | null = null;
  #status: 'none' | 'running' | 'exited' = 'none';
  #exitCode = 'worker_exited';
  #exitReason = '';
  #lastState: JsonObject | null = null;

  constructor(options: WorkerHostOptions) {
    this.#options = options;
  }

  /** True while a worker process is alive. */
  get running(): boolean {
    return this.#live() !== null;
  }

  /**
   * One renderer command. Resolves with the worker's response ({ok:true,state} or {ok:false,error});
   * a stop also aborts every in-flight AI request of the current worker.
   */
  async command(payload: unknown): Promise<WorkerReply> {
    let command;
    try {
      command = parseCommandPayload(payload);
    } catch (error) {
      return failure('invalid_command', describeError(error));
    }
    const { name, body } = command;
    let worker = this.#live();
    if (!worker) {
      if (name === 'stop' || name === 'close') {
        return this.#status === 'exited'
          ? failure(this.#exitCode, this.#exitReason)
          : failure('worker_not_running', 'The worker is not running.');
      }
      if (this.#status === 'exited' && name !== 'start' && name !== 'preset') {
        // After a crash only an explicit start/preset launches a new worker; nothing is replayed.
        if (name === 'state' && this.#lastState) return { ok: true, state: this.#exitedView() };
        return failure(this.#exitCode, this.#exitReason);
      }
      try {
        worker = this.#spawn();
      } catch (error) {
        this.#status = 'exited';
        this.#exitCode = 'worker_unavailable';
        this.#exitReason = `Could not start the worker: ${describeError(error)}`;
        return failure(this.#exitCode, this.#exitReason);
      }
    }
    if (name === 'close') worker.expectedExit = true;
    const reply = this.#send(worker, name, body);
    if (name === 'stop') this.#abortAI(worker);
    return reply;
  }

  /** The protected session changed: stop the running decision or loop and abort its AI requests. */
  invalidateSession(): void {
    const worker = this.#live();
    if (!worker) return;
    void this.#send(worker, 'stop', {});
    this.#abortAI(worker);
  }

  /** Close the worker (it closes its Chrome and daemon), terminate it if it does not exit, clean up. */
  async shutdown(timeoutMs = CLOSE_TIMEOUT_MS): Promise<void> {
    const worker = this.#worker;
    if (!worker) return;
    if (!worker.exited) {
      worker.expectedExit = true;
      void this.#send(worker, 'close', {});
      this.#abortAI(worker);
      if (!(await settlesWithin(worker.done, timeoutMs))) {
        this.#log('the worker did not exit after close; terminating it');
        terminateProcessTree(worker.child);
        await settlesWithin(worker.done, KILL_WAIT_MS);
      }
    }
    await worker.done;
  }

  #live(): WorkerProcess | null {
    const worker = this.#worker;
    return worker && !worker.exited ? worker : null;
  }

  #log(message: string): void {
    this.#options.log?.(message);
  }

  #exitedView(): JsonObject {
    return { ...this.#lastState, busy: null, status: 'blocked', blocked_reason: this.#exitReason, decision: null };
  }

  #spawn(): WorkerProcess {
    const { launch } = this.#options;
    if (!existsSync(launch.executable)) throw new Error(`${launch.missingHint} (${launch.executable})`);
    const runDir = createRunDir(this.#options.tempRoot);
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(launch.executable, [...launch.args], {
        cwd: launch.cwd,
        env: workerEnvironment(runDir, this.#options.environment),
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch (error) {
      void removeRunDir(runDir, this.#options.log);
      throw error;
    }
    let finish = () => {};
    const done = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const worker: WorkerProcess = {
      child,
      runDir,
      pending: new Map(),
      ai: new Map(),
      done,
      nextCommandId: 1,
      expectedExit: false,
      exited: false,
      violation: null,
      spawnError: null,
      stderrTail: '',
      finish,
    };
    const decoder = new LineDecoder();
    child.stdout.on('data', (chunk: Buffer) => {
      if (worker.exited || worker.violation) return;
      try {
        for (const line of decoder.push(chunk)) {
          this.#dispatch(worker, parseWorkerMessage(line));
          if (worker.violation) return;
        }
      } catch (error) {
        this.#violate(worker, describeError(error));
      }
    });
    child.stdout.once('end', () => {
      if (worker.violation) return;
      try {
        decoder.end();
      } catch (error) {
        this.#violate(worker, describeError(error));
      }
    });
    // stderr carries logs only; it is decoded leniently and never parsed.
    const stderr = new TextDecoder('utf-8');
    let partial = '';
    child.stderr.on('data', (chunk: Buffer) => {
      const text = stderr.decode(chunk, { stream: true });
      worker.stderrTail = (worker.stderrTail + text).slice(-STDERR_TAIL_CHARS);
      const lines = (partial + text).split(/\r?\n/u);
      partial = boundedText(lines.pop() ?? '', STDERR_TAIL_CHARS);
      for (const line of lines) if (line.trim()) this.#log(`worker: ${line}`);
    });
    child.stdin.on('error', (error) => this.#log(`worker stdin: ${describeError(error)}`));
    child.once('error', (error) => {
      if (child.pid !== undefined) {
        this.#log(`worker process error: ${describeError(error)}`);
        return;
      }
      worker.spawnError = describeError(error);
      this.#onExit(worker, 'it could not start');
    });
    child.once('close', (code, signal) => this.#onExit(worker, signal ? `signal ${signal}` : `exit code ${code}`));
    this.#worker = worker;
    this.#status = 'running';
    return worker;
  }

  #write(worker: WorkerProcess, message: JsonObject): boolean {
    if (worker.exited || worker.child.stdin.destroyed || !worker.child.stdin.writable) return false;
    const bytes = Buffer.from(`${JSON.stringify(message)}\n`, 'utf8');
    if (bytes.length - 1 > MAX_LINE_BYTES) return false;
    worker.child.stdin.write(bytes);
    return true;
  }

  #send(worker: WorkerProcess, name: WorkerCommandName, body: JsonObject): Promise<WorkerReply> {
    const id = worker.nextCommandId;
    worker.nextCommandId += 1;
    return new Promise((resolve) => {
      worker.pending.set(id, resolve);
      if (!this.#write(worker, { type: 'command', id, name, body })) {
        worker.pending.delete(id);
        resolve(failure('worker_exited', 'The worker is no longer accepting commands.'));
      }
    });
  }

  #dispatch(worker: WorkerProcess, message: WorkerMessage): void {
    if (message.type === 'response') {
      const resolve = worker.pending.get(message.id);
      if (!resolve) throw new ProtocolViolation(`a response to unknown command ${message.id}`);
      worker.pending.delete(message.id);
      if (message.reply.ok) this.#lastState = message.reply.state;
      resolve(message.reply);
      return;
    }
    if (message.type === 'event') {
      this.#lastState = message.state;
      this.#emit(message.state);
      return;
    }
    this.#aiRequest(worker, message);
  }

  #emit(state: JsonObject): void {
    try {
      this.#options.onState?.(state);
    } catch (error) {
      this.#log(`state listener failed: ${describeError(error)}`);
    }
  }

  #aiRequest(worker: WorkerProcess, message: Extract<WorkerMessage, { type: 'ai_request' }>): void {
    const { id } = message;
    if (worker.ai.has(id)) throw new ProtocolViolation(`a duplicate ai_request id ${id}`);
    let prepared: PreparedScenario;
    try {
      prepared = prepareScenario(message.kind, message.spec, message.timeoutMs);
    } catch (error) {
      this.#answerAI(worker, id, {
        ok: false,
        error: { code: 'AI_INPUT_INVALID', message: `The Host rejected the ai_request: ${describeError(error)}` },
      });
      return;
    }
    const controller = new AbortController();
    worker.ai.set(id, controller);
    void Promise.resolve()
      .then(() => this.#options.execute(prepared.spec, { signal: controller.signal, timeoutMs: prepared.timeoutMs }))
      .then((result) => workerResult(prepared.kind, result))
      .then(
        (result) => ({ ok: true as const, result }),
        (error: unknown) => ({ ok: false as const, error: aiErrorPayload(error) }),
      )
      .then((outcome) => {
        if (worker.ai.get(id) === controller) worker.ai.delete(id);
        // Never deliver to a later worker; a stopped worker discards late results itself.
        this.#answerAI(worker, id, outcome);
      });
  }

  #answerAI(
    worker: WorkerProcess,
    id: number,
    outcome: { ok: true; result: JsonObject } | { ok: false; error: WorkerError },
  ): void {
    if (worker.exited) return;
    if (this.#write(worker, { type: 'ai_result', id, ...outcome })) return;
    this.#write(worker, {
      type: 'ai_result',
      id,
      ok: false,
      error: { code: 'host_result_too_large', message: 'The AI result exceeds the 4 MiB message bound.' },
    });
  }

  #abortAI(worker: WorkerProcess): void {
    for (const controller of worker.ai.values()) controller.abort();
    worker.ai.clear();
  }

  #violate(worker: WorkerProcess, reason: string): void {
    if (worker.violation || worker.exited) return;
    worker.violation = reason;
    this.#log(`protocol violation (${reason}); stopping the worker`);
    terminateProcessTree(worker.child);
  }

  #onExit(worker: WorkerProcess, detail: string): void {
    if (worker.exited) return;
    worker.exited = true;
    this.#abortAI(worker);
    const lastLine = worker.stderrTail.trim().split(/\r?\n/u).at(-1)?.trim() ?? '';
    const hint = lastLine ? ` Last log line: ${boundedText(lastLine, 300)}` : '';
    let code = 'worker_exited';
    let reason = 'The worker closed.';
    if (worker.spawnError) {
      code = 'worker_unavailable';
      reason = `Could not start the worker: ${worker.spawnError}`;
    } else if (worker.violation) {
      code = 'worker_protocol_error';
      reason = `The worker broke the protocol (${worker.violation}) and was stopped. Nothing was replayed; Start launches a new worker.`;
    } else if (!worker.expectedExit) {
      reason = `The worker exited unexpectedly (${detail}). Nothing was replayed; Start launches a new worker.${hint}`;
    }
    if (code !== 'worker_exited' || !worker.expectedExit) this.#log(reason);
    for (const resolve of worker.pending.values()) resolve(failure(code, reason));
    worker.pending.clear();
    if (this.#worker === worker) {
      if (worker.expectedExit && !worker.violation) {
        this.#status = 'none';
      } else {
        this.#status = 'exited';
        this.#exitCode = code;
        this.#exitReason = reason;
        if (this.#lastState) this.#emit(this.#exitedView());
      }
    }
    void removeRunDir(worker.runDir, this.#options.log).then(worker.finish);
  }
}

function settlesWithin(promise: Promise<void>, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    void promise.then(() => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}
