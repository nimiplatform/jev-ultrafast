import { existsSync } from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const root = path.resolve(import.meta.dirname, '..');
const bin = process.platform === 'win32' ? 'Scripts' : 'bin';
const tools = path.join(root, '.nimi/local/python-tools');
const python = path.join(tools, bin, process.platform === 'win32' ? 'python.exe' : 'python');
const uv = path.join(tools, bin, process.platform === 'win32' ? 'uv.exe' : 'uv');

function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit', windowsHide: true });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Jev worker setup failed (${result.status})`);
}

if (!existsSync(uv)) {
  run('python', ['-m', 'venv', tools]);
  run(python, ['-m', 'pip', 'install', 'uv==0.12.22']);
}
run(uv, ['sync', '--frozen', '--python', '3.12.13', '--group', 'build']);
