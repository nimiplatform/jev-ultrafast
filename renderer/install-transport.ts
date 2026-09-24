import { invoke, listenShell } from '@nimiplatform/kit/shell/renderer/bridge';
import { createJevTransport, type JevTransport } from './transport.js';

declare global {
  interface Window {
    jevTransport?: JevTransport;
  }
}

// The one transport the inspector talks to, over the Kit preload bridge; it cannot be replaced later.
Object.defineProperty(window, 'jevTransport', {
  value: createJevTransport({ invoke, listen: listenShell }),
  enumerable: true,
  configurable: false,
  writable: false,
});
