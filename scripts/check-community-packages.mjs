#!/usr/bin/env node
// The standalone registry check shares the website's dependency-free validator.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { validatePackageEntries } from '../website/src/data/community-packages.mjs';
export { PACKAGE_TYPES, REQUIRED_FIELDS, validatePackage, validatePackageEntries } from '../website/src/data/community-packages.mjs';

const scriptRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export function validateRegistry(root = scriptRoot) {
  const directory = path.join(root, 'community', 'packages');
  let files;
  try {
    files = fs.readdirSync(directory).filter(file => file.endsWith('.json')).sort();
  } catch {
    return { entries: [], failures: ['community/packages/ is missing or unreadable.'] };
  }
  const parsed = [], failures = [];
  for (const file of files) {
    try {
      parsed.push([file, JSON.parse(fs.readFileSync(path.join(directory, file), 'utf8'))]);
    } catch (error) {
      failures.push(`${file}: not valid JSON (${error.message}).`);
    }
  }
  const result = validatePackageEntries(parsed);
  return { entries: result.entries, failures: [...failures, ...result.failures] };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const rootFlag = process.argv.indexOf('--root');
  const root = rootFlag === -1 ? scriptRoot : path.resolve(process.argv[rootFlag + 1] || '');
  const { entries, failures } = validateRegistry(root);
  for (const failure of failures) console.error(`✗ ${failure}`);
  if (failures.length) {
    console.error(`\ncommunity registry: ${failures.length} problem(s) across ${entries.length} package(s).`);
    process.exitCode = 1;
  } else {
    console.log(`community registry: ${entries.length} package(s) OK (${entries.map(entry => entry.name).join(', ')}).`);
  }
}
