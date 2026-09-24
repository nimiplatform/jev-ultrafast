// build:electron:production — clean, renderer (Vite), production Host, packaged worker (PyInstaller),
// worker copied into the production resources, then the Electron packager (dist-electron-package).
import { execFileSync } from 'node:child_process';
import { cp, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const script = (name, ...args) =>
  execFileSync(process.execPath, [path.join(root, 'scripts', name), ...args], { cwd: root, stdio: 'inherit', windowsHide: true });

script('clean-electron-production.mjs');
await build({ configFile: path.join(root, 'vite.config.ts'), mode: 'production', logLevel: 'warn' });
script('build-host.mjs', '--production');
script('build-worker.mjs');
// Shipped as resources/jev-worker next to app.asar; the production Host starts it from there.
const resources = path.join(root, '.nimi', 'local', 'production-resources');
await mkdir(resources, { recursive: true });
await cp(path.join(root, 'dist-python', 'jev-worker'), path.join(resources, 'jev-worker'), { recursive: true });
script('package-electron-production.mjs');
