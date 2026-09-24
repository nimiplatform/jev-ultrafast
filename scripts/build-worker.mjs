// Packaged worker: a PyInstaller --onedir `jev-worker` from the project .venv, written to
// dist-python/jev-worker. Work and spec files stay under .nimi/local. The executable also serves as the
// Browser Harness daemon (worker.main handles `-m browser_harness.daemon` when frozen), so the daemon
// and every CDP domain module are collected; snapshot.js and the fixture page are package data.
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PYINSTALLER = ['pyinstaller==6.22.3', 'pyinstaller-hooks-contrib==2026.7'];
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const windows = process.platform === 'win32';
const python = path.join(root, '.venv', windows ? 'Scripts' : 'bin', windows ? 'python.exe' : 'python');
if (!existsSync(python)) throw new Error('The project .venv is missing; create it with `uv sync` first.');
const env = { ...process.env, PYTHONUTF8: '1', PYTHONNOUSERSITE: '1' };
const run = (args) => execFileSync(python, args, { cwd: root, env, stdio: 'inherit', windowsHide: true });

const installed = spawnSync(python, ['-m', 'PyInstaller', '--version'], { cwd: root, env, encoding: 'utf8', windowsHide: true });
if (installed.status !== 0 || installed.stdout.trim() !== PYINSTALLER[0].split('==')[1]) {
  // Build tooling only, installed into the project venv; it is not an App dependency.
  run(['-m', 'pip', 'install', '--disable-pip-version-check', ...PYINSTALLER]);
}

const work = path.join(root, '.nimi', 'local', 'pyinstaller');
const distPath = path.join(root, 'dist-python');
const data = (source, target) => ['--add-data', `${path.join(root, source)}${path.delimiter}${target}`];
run([
  '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console', '--name', 'jev-worker',
  '--distpath', distPath, '--workpath', path.join(work, 'work'), '--specpath', work,
  '--paths', root,
  '--collect-submodules', 'browser_harness', '--collect-data', 'browser_harness',
  '--copy-metadata', 'browser-harness',
  '--collect-submodules', 'cdp_use',
  '--hidden-import', 'browser_harness.daemon',
  ...data('jev_ultrafast/snapshot.js', 'jev_ultrafast'),
  ...data('jev_ultrafast/static/fixture.html', 'jev_ultrafast/static'),
  path.join(root, 'scripts', 'jev_worker_entry.py'),
]);

const output = path.join(distPath, 'jev-worker');
for (const required of [
  windows ? 'jev-worker.exe' : 'jev-worker',
  '_internal/jev_ultrafast/snapshot.js',
  '_internal/jev_ultrafast/static/fixture.html',
]) {
  if (!existsSync(path.join(output, required))) throw new Error(`The packaged worker lacks ${required}.`);
}
process.stdout.write(`[build:worker] ${output}\n`);
