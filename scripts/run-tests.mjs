// test:app — the offline suite (no model, network or Chrome): ruff and pytest through the project
// .venv, the Node renderer/transport/Host tests, and syntax checks of the browser-side scripts.
import { spawnSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const windows = process.platform === 'win32';
const python = path.join(root, '.venv', windows ? 'Scripts' : 'bin', windows ? 'python.exe' : 'python');
if (!existsSync(python)) throw new Error('The project .venv is missing; create it with `uv sync` first.');
const nodeTests = readdirSync(path.join(root, 'tests'))
  .filter((name) => name.endsWith('.test.mjs'))
  .map((name) => path.join('tests', name));
const steps = [
  ['ruff', python, ['-m', 'ruff', 'check', '.']],
  ['pytest', python, ['-m', 'pytest', '-q']],
  ['node tests', process.execPath, ['--test', ...nodeTests]],
  ['app.js syntax', process.execPath, ['--check', 'jev_ultrafast/static/app.js']],
  ['snapshot.js syntax', process.execPath, ['--check', 'jev_ultrafast/snapshot.js']],
];
for (const [label, command, args] of steps) {
  process.stdout.write(`\n[test:app] ${label}\n`);
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    process.stderr.write(`[test:app] ${label} failed\n`);
    process.exit(result.status ?? 1);
  }
}
process.stdout.write('\n[test:app] all passed\n');
