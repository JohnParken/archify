// Filesystem and portable-name checks shared only by the build tools.
import fs from 'node:fs';
import path from 'node:path';
import { createHash, randomBytes } from 'node:crypto';

export function validatePortableNames(names) {
  const prefixes = new Map();
  const leaves = new Set(names);
  if (leaves.size !== names.length) throw new Error('duplicate package path');
  for (const name of names) {
    if (typeof name !== 'string' || !name || name.startsWith('/') || name.includes('\\')) throw new Error(`invalid portable path: ${name}`);
    const parts = name.split('/');
    let prefix = '';
    for (const [index, part] of parts.entries()) {
      if (!part || ['.', '..'].includes(part) || /[\x00-\x1f\x7f<>:"|?*]/u.test(part)
          || /[. ]$/u.test(part) || /[\ud800-\udfff]/u.test(part)
          || Buffer.byteLength(part) > 255 || part.length > 255
          || /^(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/iu.test(part)
          || /~[1-9][0-9]*(?:\.|$)/u.test(part)) throw new Error(`unsafe portable path component: ${name}`);
      prefix = prefix ? `${prefix}/${part}` : part;
      const key = prefix.normalize('NFC').toLowerCase();
      if (prefixes.has(key) && prefixes.get(key) !== prefix) throw new Error(`paths collide under portable filesystem semantics: ${name}`);
      prefixes.set(key, prefix);
      if (index < parts.length - 1 && leaves.has(prefix)) throw new Error(`package file is also a directory: ${prefix}`);
    }
  }
}

export function validateArchiveOutput(raw) {
  if (typeof raw !== 'string' || !raw || !raw.endsWith('.zip')) throw new Error('archive output must be a .zip filesystem path');
  const root = path.parse(raw).root;
  const relative = raw.slice(root.length);
  const comparable = process.platform === 'win32' ? relative.replaceAll('\\', '/') : relative;
  validatePortableNames([comparable]);
  return path.resolve(raw);
}

export function physicalOutputPath(raw) {
  const requested = path.resolve(raw);
  let ancestor = path.dirname(requested);
  const missing = [];
  while (!fs.existsSync(ancestor)) {
    missing.unshift(path.basename(ancestor));
    const parent = path.dirname(ancestor);
    if (parent === ancestor) throw new Error('output parent cannot be resolved');
    ancestor = parent;
  }
  return path.join(fs.realpathSync(ancestor), ...missing, path.basename(requested));
}

function state(metadata) {
  return [metadata.dev, metadata.ino, metadata.size, metadata.mtimeNs, metadata.ctimeNs, metadata.mode, metadata.nlink].join(':');
}

export function readRegular(root, relative) {
  validatePortableNames([relative]);
  let current = root;
  const ancestors = [];
  const parts = relative.split('/');
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    const metadata = fs.lstatSync(current, { bigint: true });
    if (metadata.isSymbolicLink()) throw new Error(`refusing to package symlink: ${relative}`);
    if (index < parts.length - 1) {
      if (!metadata.isDirectory()) throw new Error(`non-directory source ancestor: ${relative}`);
      ancestors.push([current, metadata.dev, metadata.ino]);
    } else if (!metadata.isFile() || metadata.nlink !== 1n) throw new Error(`source is not a single-link regular file: ${relative}`);
  }
  const before = fs.lstatSync(current, { bigint: true });
  const descriptor = fs.openSync(current, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0) | (fs.constants.O_NONBLOCK ?? 0));
  try {
    if (state(fs.fstatSync(descriptor, { bigint: true })) !== state(before)) throw new Error(`source changed before read: ${relative}`);
    const content = fs.readFileSync(descriptor);
    if (state(fs.fstatSync(descriptor, { bigint: true })) !== state(before)
        || state(fs.lstatSync(current, { bigint: true })) !== state(before)) throw new Error(`source changed while reading: ${relative}`);
    for (const [directory, dev, ino] of ancestors) {
      const now = fs.lstatSync(directory, { bigint: true });
      if (!now.isDirectory() || now.isSymbolicLink() || now.dev !== dev || now.ino !== ino) throw new Error(`source ancestor changed: ${relative}`);
    }
    return content;
  } finally { fs.closeSync(descriptor); }
}

function outputState(target) {
  let metadata;
  try { metadata = fs.lstatSync(target, { bigint: true }); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1n) throw new Error(`output is not a single-link regular file: ${target}`);
  return state(metadata) + ':' + createHash('sha256').update(readRegular(path.dirname(target), path.basename(target))).digest('hex');
}

export function publishArchive(requested, bytes) {
  const raw = validateArchiveOutput(requested);
  fs.mkdirSync(path.dirname(raw), { recursive: true });
  const parent = fs.realpathSync(path.dirname(raw));
  const parentState = fs.statSync(parent, { bigint: true });
  const target = path.join(parent, path.basename(raw));
  const lock = path.join(parent, '.archify-zip-build.lock');
  const lockFd = fs.openSync(lock, 'wx', 0o600);
  const lockState = fs.fstatSync(lockFd, { bigint: true });
  let temporary;
  let temporaryState;
  try {
    const expected = outputState(target);
    temporary = path.join(parent, `.archify-zip-${randomBytes(12).toString('hex')}.tmp`);
    const descriptor = fs.openSync(temporary, 'wx', 0o644);
    try {
      temporaryState = fs.fstatSync(descriptor, { bigint: true });
      fs.writeFileSync(descriptor, bytes);
      fs.fsyncSync(descriptor);
    } finally { fs.closeSync(descriptor); }
    const now = fs.statSync(parent, { bigint: true });
    if (fs.realpathSync(path.dirname(raw)) !== parent || now.dev !== parentState.dev || now.ino !== parentState.ino
        || outputState(target) !== expected || !readRegular(parent, path.basename(temporary)).equals(bytes)) throw new Error('archive output changed before publication');
    fs.renameSync(temporary, target);
    temporary = null;
    if (!readRegular(parent, path.basename(target)).equals(bytes)) throw new Error('published archive integrity mismatch');
    return target;
  } finally {
    if (temporary && temporaryState) {
      const now = fs.lstatSync(temporary, { bigint: true });
      if (now.dev === temporaryState.dev && now.ino === temporaryState.ino) fs.unlinkSync(temporary);
    }
    fs.closeSync(lockFd);
    const now = fs.lstatSync(lock, { bigint: true });
    if (now.dev === lockState.dev && now.ino === lockState.ino) fs.unlinkSync(lock);
    else throw new Error('archive lock ownership changed; lock preserved');
  }
}
