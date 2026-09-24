// window.jevTransport over the Kit renderer bridge. The contract is the one app.js documents:
//   command(name, body) -> Promise<state>, rejecting with an Error carrying .code
//   onState(listener)   -> unsubscribe; the worker's state events (start, progress, stop, end)
// The Host answers the one App command with {ok:true,state} or {ok:false,error:{code,message}}.

/** Must equal WORKER_COMMAND / WORKER_STATE_EVENT in src-electron/worker-host.ts (checked by the tests). */
export const WORKER_COMMAND = 'jev.worker.command';
export const WORKER_STATE_EVENT = 'jev.worker.state';

export type WorkerState = Record<string, unknown>;
export type TransportError = Error & { code: string };
export type RendererBridge = {
  readonly invoke: (command: string, payload?: unknown) => Promise<unknown>;
  readonly listen: (eventName: string, handler: (event: { payload: unknown }) => void) => Promise<() => void>;
};
export type JevTransport = {
  readonly command: (name: string, body?: Record<string, unknown>) => Promise<WorkerState>;
  readonly onState: (listener: (state: WorkerState) => void) => () => void;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function transportError(code: string, message: string): TransportError {
  return Object.assign(new Error(message), { code });
}

/** The Host bridge itself failed (not a worker error): keep its reason code. */
function bridgeFailure(error: unknown): TransportError {
  const record = isRecord(error) ? error : {};
  const code = [record.reasonCode, record.code].find(
    (value): value is string => typeof value === 'string' && value !== '',
  );
  const message = typeof record.message === 'string' && record.message ? record.message : String(error);
  return transportError(code ?? 'host_error', message);
}

export function createJevTransport(bridge: RendererBridge): JevTransport {
  return Object.freeze({
    async command(name: string, body: Record<string, unknown> = {}): Promise<WorkerState> {
      let reply: unknown;
      try {
        reply = await bridge.invoke(WORKER_COMMAND, { name, body });
      } catch (error) {
        throw bridgeFailure(error);
      }
      if (isRecord(reply) && reply.ok === true && isRecord(reply.state)) return reply.state;
      const failure = isRecord(reply) && reply.ok === false && isRecord(reply.error) ? reply.error : null;
      if (failure && typeof failure.code === 'string' && typeof failure.message === 'string') {
        throw transportError(failure.code, failure.message);
      }
      throw transportError('host_protocol_error', 'The Host returned an invalid worker reply.');
    },
    onState(listener: (state: WorkerState) => void): () => void {
      let unsubscribe: (() => void) | null = null;
      let closed = false;
      bridge
        .listen(WORKER_STATE_EVENT, (event) => {
          if (!closed && isRecord(event?.payload)) listener(event.payload);
        })
        .then(
          (off) => {
            if (closed) off();
            else unsubscribe = off;
          },
          (error: unknown) => console.error('[jev-ultrafast] cannot subscribe to worker state', error),
        );
      return () => {
        closed = true;
        unsubscribe?.();
        unsubscribe = null;
      };
    },
  });
}
