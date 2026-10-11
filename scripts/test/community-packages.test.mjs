import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { validateRegistry } from '../check-community-packages.mjs';

const script = fileURLToPath(new URL('../check-community-packages.mjs', import.meta.url));

test('standalone community CLI preserves validation and nonzero exits without website dependencies', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'archify-registry-test-'));
  try {
    const directory = path.join(root, 'community/packages');
    fs.mkdirSync(directory, { recursive: true });
    const file = path.join(directory, 'sample-skill.json');
    const entry = {
      name: 'sample-skill', type: 'skill', summary: { en: 'A sample skill for testing.', zh: '用于测试的示例技能。' },
      author: { name: 'Test' }, repository: 'https://example.test/source', archify: '3.0.1', schemaVersions: [1],
    };
    const run = () => spawnSync(process.execPath, [script, '--root', root], { encoding: 'utf8' });
    fs.writeFileSync(file, JSON.stringify(entry));
    assert.equal(run().status, 0);
    assert.deepEqual(validateRegistry(root).failures, []);
    entry.repository = 'http://example.test/source';
    fs.writeFileSync(file, JSON.stringify(entry));
    const invalid = run();
    assert.equal(invalid.status, 1);
    assert.match(invalid.stderr, /repository must be an https:\/\/ URL/);
    fs.writeFileSync(file, '{');
    assert.equal(run().status, 1);
    assert.match(validateRegistry(root).failures.join('\n'), /not valid JSON/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
