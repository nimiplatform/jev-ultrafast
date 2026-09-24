// A scripted stand-in for `python -m jev_ultrafast.worker`, used only by tests/host.test.mjs to drive the
// Host's framing and protocol code. It speaks the worker's line protocol (strict UTF-8 JSON lines) and
// never runs a browser or a model. argv[2] selects a misbehaviour for the `tick` command.
const mode = process.argv[2] || 'echo';
const decoder = new TextDecoder('utf-8', { fatal: true });
const received = [];
const waiting = new Map(); // ai_request id -> resolve
let nextAi = 1;
let stopWaiting = null;
let buffer = '';
let state = { busy: null, status: 'idle', goal: null, page: null, history: [], received };

function write(text) {
  process.stdout.write(Buffer.from(`${text}\n`, 'utf8'));
}
function send(message) {
  write(JSON.stringify(message));
}
function respond(id, extra) {
  send({ type: 'response', id, ...extra });
}
function ask(kind, spec, timeoutMs = 60000) {
  const id = nextAi++;
  send({ type: 'ai_request', id, kind, spec, timeoutMs });
  return new Promise((resolve) => waiting.set(id, resolve));
}

const DECIDE = {
  type: 'text-decide',
  state: { json: { page: { title: '旅行 · 目的地' }, elements: [], recent_actions: [] } },
  questions: [{
    id: 'operation',
    kind: 'choice',
    instructions: { json: { goal: '输入目的地 🚀', rules: 'pick one' } },
    candidates: [{ id: 'TYPE_TEXT', description: { text: '输入文字' } }, { id: 'WAIT', description: { text: 'Wait' } }],
  }],
};
const GENERATE = {
  type: 'text-generate',
  systemPrompt: 'Return {"text": value}.',
  input: [{ role: 'user', content: JSON.stringify({ goal: '输入目的地 🚀', field: { label: 'Destination' } }) }],
  responseFormat: {
    kind: 'json_schema',
    schemaName: 'jev_field_text',
    strict: true,
    jsonSchema: { type: 'object', properties: { text: { type: ['string', 'null'] } }, required: ['text'], additionalProperties: false },
  },
  maxTokens: 1024,
};

async function command({ id, name, body }) {
  switch (name) {
    case 'state':
      return respond(id, { ok: true, state });
    case 'start':
    case 'preset': {
      send({ type: 'event', name: 'state', state: { ...state, busy: name } });
      state = { ...state, status: 'ready', goal: body.goal ?? null, page: { url: body.url ?? 'fixture', title: '里斯本' } };
      // Split the response inside a 4-byte UTF-8 character: the Host must reassemble it.
      const bytes = Buffer.from(`${JSON.stringify({ type: 'response', id, ok: true, state })}\n`, 'utf8');
      const cut = bytes.indexOf(Buffer.from('🚀', 'utf8')) + 2;
      process.stdout.write(bytes.subarray(0, cut > 1 ? cut : 1));
      setTimeout(() => process.stdout.write(bytes.subarray(cut > 1 ? cut : 1)), 30);
      return undefined;
    }
    case 'tick': {
      if (mode === 'invalid-utf8') return void process.stdout.write(Buffer.from([0x7b, 0x22, 0xff, 0xfe, 0x22, 0x7d, 0x0a]));
      if (mode === 'truncated-utf8') return void process.stdout.write(Buffer.from([0x7b, 0xe4, 0xb8, 0x0a]));
      if (mode === 'oversized') return void write(`"${'x'.repeat(4 * 1024 * 1024)}"`);
      if (mode === 'not-json') return void write('not json');
      if (mode === 'unknown-response') return respond(id + 1000, { ok: true, state });
      if (mode === 'bad-ai-request') {
        received.push(await ask('decide', DECIDE, 0));
        return respond(id, { ok: true, state });
      }
      received.push(await ask('decide', DECIDE));
      received.push(await ask('generate_text', GENERATE));
      state = { ...state, history: [{ step: 1, text: '里斯本 🚀' }] };
      return respond(id, { ok: true, state });
    }
    case 'predict': {
      const answered = ask('decide', DECIDE);
      const outcome = await Promise.race([answered, new Promise((resolve) => { stopWaiting = resolve; })]);
      stopWaiting = null;
      if (outcome === 'stopped') {
        answered.then((late) => received.push({ late }));
        state = { ...state, status: 'stopped' };
        return respond(id, { ok: false, error: { code: 'stopped', message: 'Stopped. Nothing further was executed.' } });
      }
      received.push(outcome);
      return respond(id, { ok: true, state });
    }
    case 'stop':
      stopWaiting?.('stopped');
      respond(id, { ok: true, state });
      return send({ type: 'event', name: 'state', state });
    case 'act':
      if (mode === 'crash') process.exit(3);
      return respond(id, { ok: false, error: { code: 'rejected', message: 'Observe and choose before acting' } });
    case 'close':
      state = { ...state, status: 'idle', page: null };
      respond(id, { ok: true, state });
      return void setTimeout(() => process.exit(0), 20);
    default:
      return respond(id, { ok: false, error: { code: 'invalid_command', message: `unknown command ${name}` } });
  }
}

process.stdin.on('data', (chunk) => {
  buffer += decoder.decode(chunk, { stream: true });
  let newline;
  while ((newline = buffer.indexOf('\n')) >= 0) {
    const message = JSON.parse(buffer.slice(0, newline));
    buffer = buffer.slice(newline + 1);
    if (message.type === 'ai_result') {
      const resolve = waiting.get(message.id);
      waiting.delete(message.id);
      const { type: _type, id: _id, ...outcome } = message;
      if (resolve) resolve(outcome);
      else received.push({ unknown: message.id });
    } else {
      void command(message);
    }
  }
});
process.stdin.on('end', () => process.exit(0));
