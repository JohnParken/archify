#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { encodeZip } from './zip-format.mjs';
import { readRegular, publishArchive, physicalOutputPath, validateArchiveOutput, validatePortableNames } from './package-files.mjs';

try {
  const args = process.argv.slice(2);
  if (args[0] === '--validate-output') {
    if (args.length !== 2) throw new Error('usage: --validate-output <output.zip>');
    validateArchiveOutput(args[1]);
  } else {
    if (args.length !== 4 || args[2] !== '--mode-manifest') throw new Error('usage: write-deterministic-zip.mjs <directory> <output.zip> --mode-manifest <modes.json>');
    const [rootArg, outputArg, , manifestArg] = args;
    const output = physicalOutputPath(validateArchiveOutput(outputArg));
    if (process.versions.node.split('.')[0] !== '22' || process.versions.zlib !== '1.3.1-e00f703') throw new Error('canonical ZIP encoding requires Node 22 with bundled zlib 1.3.1-e00f703');
    const root = fs.realpathSync(rootArg);
    if (output === root || output.startsWith(root + path.sep)) throw new Error('archive output must be outside staged source');
    const modes = JSON.parse(readRegular(fs.realpathSync(path.dirname(path.resolve(manifestArg))), path.basename(manifestArg)).toString('utf-8'));
    if (!modes || typeof modes !== 'object' || Array.isArray(modes)) throw new Error('mode manifest must be a JSON object');
    validatePortableNames(Object.keys(modes));
    const files = [];
    function walk(relative = '') {
      const directory = path.join(root, relative);
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const inside = relative ? `${relative}/${entry.name}` : entry.name;
        if (entry.isSymbolicLink()) throw new Error(`refusing to archive symlink: ${inside}`);
        if (entry.isDirectory()) walk(inside);
        else if (entry.isFile()) files.push(inside);
        else throw new Error(`non-regular staged entry: ${inside}`);
      }
    }
    walk();
    const names = files.map(relative => `${path.basename(root)}/${relative}`);
    validatePortableNames(names);
    if (names.length !== Object.keys(modes).length || names.some(name => !Object.hasOwn(modes, name))) throw new Error('mode manifest must exactly match staged files');
    const entries = files.map((relative, index) => ({ name: names[index], content: readRegular(root, relative), mode: modes[names[index]] }));
    const archive = encodeZip(entries);
    console.log(`built ${publishArchive(outputArg, archive)} (${entries.length} files)`);
  }
} catch (error) { console.error(error.message); process.exitCode = 1; }
