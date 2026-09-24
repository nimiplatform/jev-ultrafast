import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { app, BrowserWindow, ipcMain, Menu, protocol, session, webContents } from 'electron';
import { configureNimiElectronAppHostProfile } from '@nimiplatform/kit/shell/electron/host-profile';

declare const __NIMI_ELECTRON_PRODUCTION__: boolean;

// Bind user data, session data and temp to the Host profile Nimi Desktop
// prepared under the selected data root, before any session or window and
// before the rest of Kit loads: a module that failed to load first would
// leave Electron on its default paths in the OS user's profile.
try {
  configureNimiElectronAppHostProfile(app);
} catch (error) {
  process.stderr.write(`[nimi-app-host-profile] ${error instanceof Error ? error.message : String(error)}\n`);
  app.exit(78);
  throw error;
}

const {
  isAllowedElectronRendererUrl,
  registerNimiElectronAppAssetProtocolScheme,
  registerNimiElectronAppBridge,
} = await import('@nimiplatform/kit/shell/electron/main');
const {
  WORKER_COMMAND,
  WORKER_STATE_EVENT,
  WorkerHost,
  resolveWorkerLaunch,
  sweepStaleRunDirs,
} = await import('./worker-host.js');

const APP_ID = 'io.github.nimiplatform.jev-ultrafast';
const APP_NAME = 'Jev Ultrafast';
const NATIVE_BUNDLE_IDENTIFIER = 'ai.nimi.apps.io.github.nimiplatform.jev-ultrafast';
const IS_PRODUCTION_BUNDLE = __NIMI_ELECTRON_PRODUCTION__;
// Every path resolves from the built Host entry (dist-electron/main.js inside the App or app.asar):
// bundling changes the source directory base.
const currentDir = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(currentDir, '..');
const preloadPath = path.join(currentDir, 'preload.cjs');
const productionRendererUrl = pathToFileURL(path.join(appRoot, 'dist', 'index.html')).toString();
const developmentRendererUrl = readDevelopmentRendererUrl();
const rendererUrl = developmentRendererUrl || productionRendererUrl;
const allowedRendererUrls = [rendererUrl];
// Development: the project .venv runs `python -m jev_ultrafast.worker` from the App root.
// Packaged: the PyInstaller onedir worker copied into the App resources (resources/jev-worker).
const workerLaunch = IS_PRODUCTION_BUNDLE
  ? resolveWorkerLaunch({ packaged: true, resourcesPath: process.resourcesPath })
  : resolveWorkerLaunch({ packaged: false, appRoot });

app.setName(APP_NAME);
app.setAppUserModelId(NATIVE_BUNDLE_IDENTIFIER);
Menu.setApplicationMenu(null);
registerNimiElectronAppAssetProtocolScheme(protocol);

const log = (message: string) => console.warn(`[jev-ultrafast] ${message}`);
type StateSink = { readonly send: (eventName: string, payload: unknown) => void; readonly destroyed: () => boolean };
// Single window: worker state events go to the renderer that sent the latest command.
let stateSink: StateSink | null = null;
let workerHost: InstanceType<typeof WorkerHost> | null = null;
let quitting = false;

void app.whenReady().then(async () => {
  const tempRoot = app.getPath('temp');
  void sweepStaleRunDirs(tempRoot, log);
  let bridge: ReturnType<typeof registerNimiElectronAppBridge> | null = null;
  const host = new WorkerHost({
    launch: workerLaunch,
    tempRoot,
    execute: (spec, options) => {
      if (!bridge) throw new Error('The Nimi App bridge is not registered.');
      return bridge.services.ai.scenario.execute(spec, options);
    },
    onState: (state) => {
      const sink = stateSink;
      if (!sink) return;
      if (sink.destroyed()) {
        stateSink = null;
        return;
      }
      sink.send(WORKER_STATE_EVENT, state);
    },
    log,
  });
  workerHost = host;
  bridge = registerNimiElectronAppBridge({
    appId: APP_ID,
    allowedRendererUrls,
    assetMediaPlatform: { protocol, webRequest: session.defaultSession.webRequest, webContents },
    ipcMain,
    appCommandHandlers: {
      [WORKER_COMMAND]: ({ payload, sendEvent, event }) => {
        if (sendEvent) {
          const sender = event.sender;
          stateSink = { send: sendEvent, destroyed: () => sender?.isDestroyed?.() === true };
        }
        return host.command(payload);
      },
    },
    // Cancel the running decision or loop and every in-flight AI request of the old session.
    onSessionInvalidated: () => host.invalidateSession(),
  });
  await createMainWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) void createMainWindow();
  });
}).catch((error: unknown) => {
  // A Host that could not register its bridge or open its window must not linger without either.
  log(`startup failed: ${error instanceof Error ? error.stack || error.message : String(error)}`);
  app.exit(1);
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

// Close the worker (it closes its automation Chrome and daemon) before the Host exits.
app.on('before-quit', (event) => {
  const host = workerHost;
  if (quitting || !host?.running) return;
  event.preventDefault();
  quitting = true;
  void host.shutdown()
    .catch((error: unknown) => log(`worker shutdown: ${error instanceof Error ? error.message : String(error)}`))
    .finally(() => app.quit());
});

async function createMainWindow(): Promise<void> {
  const window = new BrowserWindow({
    width: 1320,
    height: 900,
    minWidth: 720,
    minHeight: 560,
    title: APP_NAME,
    autoHideMenuBar: true,
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedElectronRendererUrl(url, allowedRendererUrls)) event.preventDefault();
  });
  await window.loadURL(rendererUrl);
}

function readDevelopmentRendererUrl(): string {
  const flag = '--nimi-dev-renderer-url';
  const prefix = '--nimi-dev-renderer-url=';
  const hasDevelopmentRendererArgument = process.argv.some((value) => value === flag || value.startsWith(prefix));
  if (IS_PRODUCTION_BUNDLE && hasDevelopmentRendererArgument) {
    throw new Error('The production Electron bundle rejects --nimi-dev-renderer-url.');
  }
  if (process.argv.includes(flag)) throw new Error('Nimi development renderer URL is missing.');
  const values = process.argv.filter((value) => value.startsWith(prefix));
  if (values.length === 0) return '';
  if (values.length !== 1) throw new Error('Nimi development renderer URL must be singular.');
  const selected = values[0];
  if (!selected) throw new Error('Nimi development renderer URL is missing.');
  const raw = selected.slice(prefix.length);
  const parsed = new URL(raw);
  if (
    parsed.protocol !== 'http:'
    || !['127.0.0.1', 'localhost', '[::1]', '::1'].includes(parsed.hostname.toLowerCase())
    || !parsed.port
    || parsed.username
    || parsed.password
    || (parsed.pathname !== '/' && parsed.pathname !== '')
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('Nimi development renderer URL must be exact loopback.');
  }
  return parsed.origin;
}
