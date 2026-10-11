import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { stageCleanSkill } from '../stage-clean-skill.mjs';
import { publishArchive, validatePortableNames } from '../package-files.mjs';

const temporaryRoots = [];

function temporaryRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'archify-package-test-'));
  temporaryRoots.push(root);
  return root;
}

function git(root, args) {
  const result = spawnSync('git', args, {
    cwd: root,
    encoding: 'utf8',
    env: { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1',
      GIT_AUTHOR_NAME: 'Package Test', GIT_AUTHOR_EMAIL: 'package-test@example.invalid',
      GIT_COMMITTER_NAME: 'Package Test', GIT_COMMITTER_EMAIL: 'package-test@example.invalid' },
  });
  assert.equal(result.status, 0, result.stderr || `git ${args.join(' ')} failed`);
}

function fixture() {
  const root = path.join(temporaryRoot(), 'repo');
  fs.mkdirSync(root);
  git(root, ['init', '-q']);
  for (const [relative, content] of Object.entries({
    'archify/SKILL.md': '# Skill\n',
    'archify/LICENSE': 'license\n',
    'archify/THIRD_PARTY_NOTICES.md': 'notices\n',
    'archify/skill-release.json': '{}\n',
    'archify/examples/hello.md': 'example\n',
    'archify/modules/core.md': 'module\n',
    'archify/tests/should-not-ship.py': 'test\n',
    'archify/python/archify_runtime/__pycache__/stale.pyc': 'cache\n',
  })) {
    const file = path.join(root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
  }
  git(root, ['add', '--', 'archify']);
  return root;
}

function stage(root, label = 'stage') {
  const destination = path.join(path.dirname(root), `${label}-${Math.random().toString(16).slice(2)}`);
  return { destination, result: stageCleanSkill({ repoRoot: root, destination }) };
}

test.after(() => {
  for (const root of temporaryRoots) fs.rmSync(root, { recursive: true, force: true });
});

test('stages tracked package files, excludes tests/cache and ignores untracked private files and symlinks', () => {
  const root = fixture();
  fs.writeFileSync(path.join(root, 'archify', 'private.txt'), 'private\n');
  const outside = path.join(temporaryRoot(), 'outside.txt');
  fs.writeFileSync(outside, 'external\n');
  fs.symlinkSync(outside, path.join(root, 'archify', 'external-link'));

  const { destination, result } = stage(root);
  assert.ok(result.fileCount >= 6);
  assert.equal(fs.readFileSync(path.join(destination, 'examples/hello.md'), 'utf8'), 'example\n');
  assert.equal(fs.readFileSync(path.join(destination, 'modules/core.md'), 'utf8'), 'module\n');
  for (const excluded of ['tests/should-not-ship.py', 'python/archify_runtime/__pycache__/stale.pyc', 'private.txt', 'external-link']) {
    assert.equal(fs.existsSync(path.join(destination, excluded)), false, excluded);
  }
});

test('rejects tracked symlinks and symlinked source ancestors', () => {
  const trackedLinkRoot = fixture();
  fs.symlinkSync('hello.md', path.join(trackedLinkRoot, 'archify/examples/link.md'));
  git(trackedLinkRoot, ['add', '--', 'archify/examples/link.md']);
  assert.throws(() => stageCleanSkill({ repoRoot: trackedLinkRoot, destination: path.join(path.dirname(trackedLinkRoot), 'tracked-link-out') }), /unsupported tracked mode/);

  const ancestorRoot = fixture();
  const outside = path.join(temporaryRoot(), 'outside-dir');
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'module.md'), 'external\n');
  fs.mkdirSync(path.join(ancestorRoot, 'archify', 'linked'));
  fs.writeFileSync(path.join(ancestorRoot, 'archify', 'linked', 'module.md'), 'tracked\n');
  git(ancestorRoot, ['add', '--', 'archify/linked/module.md']);
  fs.rmSync(path.join(ancestorRoot, 'archify', 'linked'), { recursive: true });
  fs.symlinkSync(outside, path.join(ancestorRoot, 'archify', 'linked'));
  assert.throws(() => stageCleanSkill({ repoRoot: ancestorRoot, destination: path.join(path.dirname(ancestorRoot), 'ancestor-link-out') }), /symlink/);
  assert.equal(fs.readFileSync(path.join(outside, 'module.md'), 'utf8'), 'external\n');
});

test('rejects destination and manifest conflicts without replacing their contents', () => {
  const root = fixture();
  const parent = temporaryRoot();
  const destination = path.join(parent, 'known-destination');
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, 'keep.txt'), 'keep destination\n');
  assert.throws(() => stageCleanSkill({ repoRoot: root, destination }), /EEXIST/);
  assert.equal(fs.readFileSync(path.join(destination, 'keep.txt'), 'utf8'), 'keep destination\n');

  const manifest = path.join(parent, 'known-manifest.json');
  fs.writeFileSync(manifest, 'keep manifest\n');
  const freshDestination = path.join(parent, 'fresh-destination');
  assert.throws(() => stageCleanSkill({ repoRoot: root, destination: freshDestination, modeManifest: manifest }), /EEXIST/);
  assert.equal(fs.readFileSync(manifest, 'utf8'), 'keep manifest\n');
});

test('rejects portable case, normalization, and file-directory prefix collisions', () => {
  assert.throws(() => validatePortableNames(['Skill/Readme.md', 'skill/readme.md']), /collide/);
  assert.throws(() => validatePortableNames(['café/file', 'cafe\u0301/file']), /collide/);
  assert.throws(() => validatePortableNames(['folder', 'folder/file']), /directory/);
});

test('publishArchive preserves output symlink/hardlink targets and rejects a foreign lock', () => {
  const root = temporaryRoot();
  const archive = Buffer.from('zip bytes');
  const symlinkTarget = path.join(root, 'symlink-target.zip');
  fs.writeFileSync(symlinkTarget, 'keep symlink target\n');
  const symlinkOutput = path.join(root, 'symlink-output.zip');
  fs.symlinkSync(symlinkTarget, symlinkOutput);
  assert.throws(() => publishArchive(symlinkOutput, archive), /single-link regular file/);
  assert.equal(fs.readFileSync(symlinkTarget, 'utf8'), 'keep symlink target\n');
  assert.equal(fs.readlinkSync(symlinkOutput), symlinkTarget);

  const hardlinkTarget = path.join(root, 'hardlink-target.zip');
  const hardlinkOutput = path.join(root, 'hardlink-output.zip');
  fs.writeFileSync(hardlinkTarget, 'keep hardlink\n');
  fs.linkSync(hardlinkTarget, hardlinkOutput);
  assert.throws(() => publishArchive(hardlinkOutput, archive), /single-link regular file/);
  assert.equal(fs.readFileSync(hardlinkTarget, 'utf8'), 'keep hardlink\n');
  assert.equal(fs.readFileSync(hardlinkOutput, 'utf8'), 'keep hardlink\n');

  const lock = path.join(root, '.archify-zip-build.lock');
  fs.writeFileSync(lock, 'foreign lock\n');
  const ordinaryOutput = path.join(root, 'ordinary.zip');
  assert.throws(() => publishArchive(ordinaryOutput, archive), /EEXIST/);
  assert.equal(fs.readFileSync(lock, 'utf8'), 'foreign lock\n');
  assert.equal(fs.existsSync(ordinaryOutput), false);
});

test('includes only the exact untracked Python runtime additions and materializes the internal alias', () => {
  const root = fixture();
  const write = (relative, content) => {
    const file = path.join(root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
  };
  write('archify/bin/archify.py', '#!/usr/bin/env python3\nprint("ok")\n');
  fs.chmodSync(path.join(root, 'archify/bin/archify.py'), 0o755);
  write('archify/python/requirements.txt', '');
  write('archify/assets/template.html', '<html></html>\n');
  write('archify/schemas/common.schema.json', '{}\n');
  fs.symlinkSync('archify.py', path.join(root, 'archify/bin/archify'));
  git(root, ['add', '--', 'archify/bin/archify.py', 'archify/bin/archify', 'archify/python/requirements.txt', 'archify/assets/template.html', 'archify/schemas/common.schema.json']);
  write('archify/python/archify_runtime/browser.py', 'browser = True\n');
  write('archify/python/archify_runtime/integrity.py', 'integrity = True\n');
  for (const name of ['cli', 'resources', 'delivery', 'repository', 'comparison', 'commands']) write(`archify/python/archify_runtime/${name}.py`, `${name} = True\n`);
  write('archify/bin/archify.mjs', '// Python compatibility launcher\n');
  write('archify/python/requirements-browser.txt', 'playwright==1.63.0\n');
  write('archify/python/archify_runtime/extra.py', 'private = True\n');
  write('work/private-analysis.txt', 'not part of the Skill\n');

  const { destination, result } = stage(root, 'python-stage');
  assert.equal(fs.readFileSync(path.join(destination, 'bin/archify'), 'utf8'), fs.readFileSync(path.join(destination, 'bin/archify.py'), 'utf8'));
  assert.equal(fs.statSync(path.join(destination, 'bin/archify')).mode & 0o111, 0o111);
  assert.equal(result.modes[`${path.basename(destination)}/bin/archify`], '100755');
  assert.equal(fs.readFileSync(path.join(destination, 'python/archify_runtime/browser.py'), 'utf8'), 'browser = True\n');
  assert.equal(fs.readFileSync(path.join(destination, 'python/archify_runtime/integrity.py'), 'utf8'), 'integrity = True\n');
  for (const name of ['cli', 'resources', 'delivery', 'repository', 'comparison', 'commands']) {
    assert.equal(fs.readFileSync(path.join(destination, `python/archify_runtime/${name}.py`), 'utf8'), `${name} = True\n`);
  }
  assert.equal(fs.readFileSync(path.join(destination, 'bin/archify.mjs'), 'utf8'), '// Python compatibility launcher\n');
  assert.equal(fs.readFileSync(path.join(destination, 'python/requirements-browser.txt'), 'utf8'), 'playwright==1.63.0\n');
  assert.equal(fs.existsSync(path.join(destination, 'python/archify_runtime/extra.py')), false);
  assert.equal(fs.existsSync(path.join(destination, 'work/private-analysis.txt')), false);

  fs.unlinkSync(path.join(root, 'archify/bin/archify'));
  fs.symlinkSync('../../outside.py', path.join(root, 'archify/bin/archify'));
  assert.throws(() => stageCleanSkill({ repoRoot: root, destination: path.join(temporaryRoot(), 'changed-alias-out') }), /unsafe bin\/archify symlink target/);
});
