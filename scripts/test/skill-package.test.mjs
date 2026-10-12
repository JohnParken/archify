import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { checkSkillPackage } from '../check-skill-package.mjs';
import { stageCleanSkill } from '../stage-clean-skill.mjs';

const description = 'Create validated, interactive diagrams from requirements, Mermaid or repository evidence. Use for architecture, processes, sequences and states.';
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'archify-compliance-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(root, 'SKILL.md'), `---\nname: archify\ndescription: ${JSON.stringify(description)}\n---\n# Archify\n`);
  return root;
}

test('valid mapping profile and Unicode character count', t => {
  const root = fixture(t);
  assert.equal(checkSkillPackage(root).status, 'passed');
  const file = path.join(root, 'SKILL.md');
  fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace(description, '图'.repeat(100)));
  assert.equal(checkSkillPackage(root).status, 'passed');
});

test('rejects invalid identity, description and frontmatter', t => {
  const root = fixture(t);
  const file = path.join(root, 'SKILL.md');
  const original = fs.readFileSync(file, 'utf8');
  for (const [before, after, expected] of [
    ['name: archify', 'name: -archify', /invalid Skill name/],
    [description, 'too short', /100–150/],
    ['name: archify', 'name: archify\nname: other', /duplicate/],
    ['name: archify', 'name: [archify]', /invalid/],
    ['name: archify', 'name: false', /invalid plain scalar/],
    ['name: archify', 'name: Archify_name', /invalid Skill name/],
  ]) {
    fs.writeFileSync(file, original.replace(before, after));
    assert.throws(() => checkSkillPackage(root), expected);
  }
});

test('enforces body length and Python requirements', t => {
  const root = fixture(t);
  const file = path.join(root, 'SKILL.md');
  const original = fs.readFileSync(file, 'utf8');
  fs.writeFileSync(file, original + 'line\n'.repeat(499));
  assert.equal(checkSkillPackage(root).status, 'passed');
  fs.appendFileSync(file, 'line\n');
  assert.throws(() => checkSkillPackage(root), /exceeds 500 lines/);
  fs.writeFileSync(file, original);
  fs.mkdirSync(path.join(root, 'python'));
  fs.writeFileSync(path.join(root, 'python/runtime.py'), 'print("example")\n');
  assert.throws(() => checkSkillPackage(root), /missing Python runtime requirements/);
  fs.writeFileSync(path.join(root, 'python/requirements.txt'), 'jsonschema>=4.20.0,<5.0.0\n');
  assert.equal(checkSkillPackage(root).status, 'passed');
});

test('a Contents heading requires links to reference sections', t => {
  const root = fixture(t);
  fs.mkdirSync(path.join(root, 'references'));
  const file = path.join(root, 'references/long.md');
  fs.writeFileSync(file, '# Reference\n\n## Contents\n\n## Usage\n' + 'line\n'.repeat(101));
  assert.throws(() => checkSkillPackage(root), /missing navigation/);
  fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace('## Contents\n', '## Contents\n\n- [Usage](#usage)\n'));
  assert.equal(checkSkillPackage(root).status, 'passed');
});

test('rejects hardcoded host paths in runtime files', t => {
  const root = fixture(t);
  const file = path.join(root, 'runtime.json');
  for (const value of ['/Users/example/private', '/home/example/private', 'C:\\Users\\example\\private']) {
    fs.writeFileSync(file, value);
    assert.throws(() => checkSkillPackage(root), /local host path/);
  }
  fs.writeFileSync(file, 'portable/path');
  assert.equal(checkSkillPackage(root).status, 'passed');
});

test('rejects missing relative resources and anchors', t => {
  const root = fixture(t);
  const file = path.join(root, 'SKILL.md');
  fs.appendFileSync(file, '[Missing](references/missing.md)\n');
  assert.throws(() => checkSkillPackage(root), /missing resource/);
  fs.mkdirSync(path.join(root, 'references'));
  fs.writeFileSync(path.join(root, 'references/missing.md'), '# Reference\n');
  fs.appendFileSync(file, '[Wrong section](references/missing.md#wrong)\n');
  assert.throws(() => checkSkillPackage(root), /missing anchor/);
});

test('rejects development files, empty content and unindexed navigation', t => {
  const root = fixture(t);
  fs.writeFileSync(path.join(root, 'README.md'), 'development\n');
  fs.writeFileSync(path.join(root, 'empty'), '');
  fs.mkdirSync(path.join(root, 'references'));
  fs.writeFileSync(path.join(root, 'references/long.md'), '# Reference\n' + 'line\n'.repeat(101));
  assert.throws(() => checkSkillPackage(root), error => /development/.test(error.message) && /empty file/.test(error.message) && /navigation/.test(error.message));
});

test('current tracked-source package meets the distribution contract', t => {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'archify-real-compliance-'));
  t.after(() => fs.rmSync(scratch, { recursive: true, force: true }));
  const destination = path.join(scratch, 'archify');
  stageCleanSkill({ repoRoot: path.resolve(import.meta.dirname, '../..'), destination });
  assert.equal(checkSkillPackage(destination).status, 'passed');
});
