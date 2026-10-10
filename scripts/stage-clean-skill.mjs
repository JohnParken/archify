#!/usr/bin/env node
// Runtime-only staging from tracked working-tree files and a narrow new-file allowlist.
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { readRegular, validatePortableNames } from './package-files.mjs';

// These two runtime additions are intentionally permitted before their first commit.
// All other untracked content, including tests, caches and generated diagrams, is excluded.
const EXTRA_RUNTIME_FILES = ['archify/python/archify_runtime/browser.py', 'archify/python/archify_runtime/integrity.py'];
const EXCLUDED_SEGMENTS = new Set(['test', 'tests', '__pycache__', 'node_modules', '.DS_Store', '.hive', '.workbuddy', '.archify']);
const EXCLUDED_FILES = new Set(['archify/package-lock.json', 'archify/scripts/generate-brand-marks.mjs', 'archify/scripts/generate-validators.mjs']);
const REQUIRED = ['archify/SKILL.md', 'archify/LICENSE', 'archify/THIRD_PARTY_NOTICES.md', 'archify/skill-release.json'];

function trackedEntries(root) {
  const result = spawnSync('git', ['ls-files', '--stage', '-z', '--', 'archify'], { cwd: root, maxBuffer: 16 * 1024 * 1024 });
  if (result.status !== 0) throw new Error(`unable to enumerate tracked Skill files: ${result.stderr?.toString() || result.error?.message}`);
  return new TextDecoder('utf-8', { fatal: true }).decode(result.stdout).split('\0').filter(Boolean).map(record => {
    const separator = record.indexOf('\t');
    const [mode, , stage] = record.slice(0, separator).split(' ');
    const relative = record.slice(separator + 1);
    if (separator < 0 || stage !== '0' || !relative.startsWith('archify/')) throw new Error(`invalid or conflicted Git entry: ${relative}`);
    return { relative, mode };
  });
}

export function stageCleanSkill({ repoRoot, destination, modeManifest = null }) {
  if (!repoRoot || !destination) throw new Error('repoRoot and destination are required');
  const root = fs.realpathSync(repoRoot);
  const entries = trackedEntries(root).filter(({ relative }) => !EXCLUDED_FILES.has(relative)
    && !relative.split('/').some(part => EXCLUDED_SEGMENTS.has(part) || part.startsWith('.validator-check-'))
    && !relative.endsWith('.pyc') && !relative.endsWith('.pyo'));
  const indexed = new Map(entries.map(entry => [entry.relative, entry]));
  const python = indexed.has('archify/bin/archify.py');
  if (python) {
    for (const relative of EXTRA_RUNTIME_FILES) if (!indexed.has(relative)) {
      entries.push({ relative, mode: '100644' });
      indexed.set(relative, entries.at(-1));
    }
  }
  const required = python ? [...REQUIRED, 'archify/bin/archify.py', 'archify/python/requirements.txt',
    'archify/assets/template.html', 'archify/schemas/common.schema.json', ...EXTRA_RUNTIME_FILES] : REQUIRED;
  for (const relative of required) if (!indexed.has(relative)) throw new Error(`required package input is missing: ${relative}`);
  validatePortableNames(entries.map(entry => entry.relative));
  const snapshots = entries.map(entry => {
    if (entry.relative === 'archify/bin/archify' && entry.mode === '120000') {
      // Materialize only this canonical internal alias; never follow arbitrary links.
      readRegular(root, 'archify/bin/archify.py');
      if (fs.readlinkSync(path.join(root, entry.relative)) !== 'archify.py') throw new Error('unsafe bin/archify symlink target');
      return { ...entry, mode: '100755', content: readRegular(root, 'archify/bin/archify.py') };
    }
    if (!['100644', '100755'].includes(entry.mode)) throw new Error(`unsupported tracked mode (symlink/submodule): ${entry.relative}`);
    let content = readRegular(root, entry.relative);
    if (entry.relative === 'archify/package.json') {
      const manifest = JSON.parse(content.toString('utf-8'));
      delete manifest.scripts;
      delete manifest.devDependencies;
      content = Buffer.from(JSON.stringify(manifest, null, 2) + '\n');
    }
    return { ...entry, content };
  });
  const requested = path.resolve(destination);
  validatePortableNames([path.basename(requested)]);
  const parent = fs.realpathSync(path.dirname(requested));
  const dest = path.join(parent, path.basename(requested));
  if (dest === root || dest.startsWith(root + path.sep)) throw new Error('staging destination must be outside the repository');
  const manifestRequested = modeManifest ? path.resolve(modeManifest) : null;
  const manifestPath = manifestRequested ? path.join(fs.realpathSync(path.dirname(manifestRequested)), path.basename(manifestRequested)) : null;
  if (manifestPath) validatePortableNames([path.basename(manifestPath)]);
  if (manifestPath && (manifestPath === dest || manifestPath.startsWith(dest + path.sep)
      || manifestPath === root || manifestPath.startsWith(root + path.sep))) throw new Error('mode manifest must be outside source and staged Skill');
  // Exclusive mkdir preserves existing destinations, including dangling symlinks.
  fs.mkdirSync(dest);
  const modes = {};
  for (const entry of snapshots) {
    const inside = entry.relative.slice('archify/'.length);
    const target = path.join(dest, ...inside.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, entry.content, { flag: 'wx', mode: entry.mode === '100755' ? 0o755 : 0o644 });
    modes[`${path.basename(dest)}/${inside}`] = entry.mode;
  }
  if (manifestPath) fs.writeFileSync(manifestPath, JSON.stringify(modes, null, 2) + '\n', { flag: 'wx', mode: 0o644 });
  return { destination: dest, fileCount: snapshots.length, modes };
}

export function isMainModule({ argvPath = process.argv[1], modulePath = fileURLToPath(import.meta.url) } = {}) {
  return Boolean(argvPath && path.resolve(argvPath) === path.resolve(modulePath));
}

if (isMainModule()) {
  try {
    const args = process.argv.slice(2);
    const allowed = new Set(['--root', '--dest', '--mode-manifest']);
    if (args.length % 2 || args.some((arg, index) => index % 2 === 0 && !allowed.has(arg))) throw new Error('usage: stage-clean-skill.mjs --root <repo> --dest <new-directory> [--mode-manifest <new-json>]');
    const options = Object.fromEntries(Array.from({ length: args.length / 2 }, (_, index) => [args[index * 2], args[index * 2 + 1]]));
    const result = stageCleanSkill({ repoRoot: options['--root'], destination: options['--dest'], modeManifest: options['--mode-manifest'] });
    console.log(JSON.stringify({ destination: result.destination, fileCount: result.fileCount }));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
