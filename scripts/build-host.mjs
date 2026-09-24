// Electron Host: dist-electron/main.js (ESM, node_modules external) and the sandboxed preload
// dist-electron/preload.cjs (Kit's preload bundled in). --production sets the production marker, which
// rejects development renderer arguments and runs the packaged worker from the App resources.
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const production = process.argv.includes('--production');

await build({
  entryPoints: [path.join(appRoot, 'src-electron/main.ts')],
  outfile: path.join(appRoot, 'dist-electron/main.js'),
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'esm',
  packages: 'external',
  external: ['electron'],
  define: { __NIMI_ELECTRON_PRODUCTION__: String(production) },
  logLevel: 'warning',
});
await build({
  entryPoints: [path.join(appRoot, 'src-electron/preload.cts')],
  outfile: path.join(appRoot, 'dist-electron/preload.cjs'),
  bundle: true,
  platform: 'node',
  target: 'node22',
  format: 'cjs',
  external: ['electron'],
  logLevel: 'warning',
});
process.stdout.write(`[build:electron] dist-electron/main.js + preload.cjs (${production ? 'production' : 'development'})\n`);
