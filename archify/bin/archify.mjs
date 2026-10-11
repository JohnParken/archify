#!/usr/bin/env node
// Compatibility launcher. Rendering and verification run in the Python runtime.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const script = fileURLToPath(new URL('./archify.py', import.meta.url));
const configured = process.env.ARCHIFY_PYTHON || process.env.PYTHON;
const choices = configured ? [[configured, []]] : process.platform === 'win32' ? [['py', ['-3']], ['python', []]] : [['python3', []], ['python', []]];
for (const [python, prefix] of choices) {
  const result = spawnSync(python, [...prefix, script, ...process.argv.slice(2)], { stdio: 'inherit' });
  if (result.error?.code === 'ENOENT') continue;
  if (result.error) { console.error(result.error.message); process.exit(1); }
  process.exit(result.status ?? 1);
}
console.error('Python 3.9+ is required; set ARCHIFY_PYTHON to its executable.');
process.exit(1);
