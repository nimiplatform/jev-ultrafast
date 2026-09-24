import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, type Plugin } from 'vite';

const projectRoot = path.dirname(fileURLToPath(import.meta.url));
const inspectorPage = path.join(projectRoot, 'jev_ultrafast', 'static', 'index.html');

// Each edit must match the upstream inspector page exactly once, or the renderer fails to build.
const PAGE_EDITS: ReadonlyArray<readonly [pattern: RegExp, replacement: string, label: string]> = [
  // No web fonts from the network in the desktop App: style.css falls back to the system UI font.
  [/^[ \t]*<link rel="stylesheet" href="https:\/\/rsms\.me\/inter\/inter\.css" \/>\r?\n/gmu, '', 'the rsms.me Inter stylesheet'],
  // style.css is bundled through the renderer entry instead.
  [/^[ \t]*<link rel="stylesheet" href="\.\/style\.css" \/>\r?\n/gmu, '', 'the style.css link'],
  // The entry installs window.jevTransport, then runs the unchanged app.js.
  [/<script type="module" src="\.\/app\.js"><\/script>/gu, '<script type="module" src="./main.ts"></script>', 'the app.js script'],
];

function inspectorHtml(source: string): string {
  let html = source;
  for (const [pattern, replacement, label] of PAGE_EDITS) {
    const found = html.match(pattern)?.length ?? 0;
    if (found !== 1) {
      throw new Error(`jev_ultrafast/static/index.html must contain ${label} exactly once (found ${found}).`);
    }
    html = html.replace(pattern, replacement);
  }
  return html;
}

/** Serve and build the upstream inspector page (renderer/index.html is only the entry placeholder). */
function inspectorPagePlugin(): Plugin {
  return {
    name: 'jev-inspector-page',
    transformIndexHtml: {
      order: 'pre',
      handler: () => inspectorHtml(readFileSync(inspectorPage, 'utf8')),
    },
    configureServer(server) {
      server.watcher.add(inspectorPage);
      server.watcher.on('change', (file) => {
        if (path.resolve(file) === inspectorPage) server.ws.send({ type: 'full-reload' });
      });
    },
  };
}

export default defineConfig({
  root: path.join(projectRoot, 'renderer'),
  base: './',
  cacheDir: path.join(projectRoot, '.vite'),
  publicDir: false,
  plugins: [inspectorPagePlugin()],
  server: { host: '127.0.0.1', port: 1533, strictPort: true },
  build: {
    outDir: path.join(projectRoot, 'dist'),
    emptyOutDir: true,
    rollupOptions: {
      treeshake: {
        // Kit and the SDK declare no `sideEffects`, so every module behind Kit's bridge index (the SDK's
        // protobuf types among them) would be kept. The renderer uses only Kit's invoke and listenShell,
        // which have no import-time effects; unused Kit/SDK modules are dropped.
        moduleSideEffects: (id) => !/[\\/]node_modules[\\/]@nimiplatform[\\/](?:kit|sdk)[\\/]/u.test(id),
      },
    },
  },
});
