#!/usr/bin/env node
// Validate the staged distribution, not repository development files.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

function anchor(title) {
  return title.toLowerCase().replace(/[^\p{L}\p{N}_ -]/gu, '').replace(/ /g, '-');
}

export function checkSkillPackage(root) {
  root = path.resolve(root);
  const errors = [];
  const files = [];
  function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name);
      const relative = path.relative(root, file);
      if (entry.isSymbolicLink()) { errors.push(`symlink: ${relative}`); continue; }
      if (/^(?:readme(?:\..*)?|test|tests|__pycache__|node_modules|\.DS_Store)$/i.test(entry.name)
          || /\.(?:py[co]|log|tmp)$/i.test(entry.name)) errors.push(`development/cache file: ${relative}`);
      if (entry.isDirectory()) {
        if (!fs.readdirSync(file).length) errors.push(`empty directory: ${relative}`);
        walk(file);
      } else {
        files.push(file);
        if (!fs.statSync(file).size) errors.push(`empty file: ${relative}`);
      }
    }
  }
  walk(root);
  const entry = path.join(root, 'SKILL.md');
  if (!files.includes(entry)) errors.push('missing uppercase SKILL.md');
  else {
    const text = fs.readFileSync(entry, 'utf8');
    const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n/.exec(text);
    if (!match) errors.push('missing YAML frontmatter delimiters');
    else {
      const body = text.slice(match[0].length).replace(/\r?\n$/, '');
      if (body.split(/\r?\n/).length > 500) errors.push('Skill body exceeds 500 lines');
      // Deliberately enforce the repository's small YAML mapping profile.
      // Quoted values use JSON double quotes, a valid YAML scalar subset.
      const fields = {};
      const seen = new Set();
      let parent = '';
      for (const line of match[1].split(/\r?\n/)) {
        const scalar = /^( {0}| {2})([A-Za-z_][\w-]*):(?: (.+))?$/.exec(line);
        if (!scalar) { errors.push(`unsupported frontmatter syntax: ${line}`); continue; }
        const [, indent, key, value] = scalar;
        if (indent && !parent) errors.push(`frontmatter nesting without mapping: ${key}`);
        const identity = indent ? `${parent}.${key}` : key;
        if (seen.has(identity)) errors.push(`duplicate frontmatter key: ${identity}`);
        seen.add(identity);
        if (!indent) parent = value === undefined ? key : '';
        if (value !== undefined) {
          let parsed = value;
          if (value.startsWith('"')) {
            try { parsed = JSON.parse(value); } catch { errors.push(`invalid quoted scalar: ${identity}`); }
          } else if (!/^[A-Za-z0-9_./() ,+-]+$/.test(value)
              || /^(?:null|true|false|~|[-+]?\d+(?:\.\d+)?)$/i.test(value)) errors.push(`invalid plain scalar: ${identity}`);
          if (!indent) fields[key] = parsed;
        }
      }
      if (typeof fields.name !== 'string' || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(fields.name) || fields.name.length > 64) errors.push('invalid Skill name');
      const length = typeof fields.description === 'string' ? [...fields.description].length : 0;
      if (length < 100 || length > 150) errors.push(`description must contain 100–150 Unicode characters (got ${length})`);
      if (fields.dependency !== undefined && (typeof fields.dependency !== 'string' || !fields.dependency.trim())) errors.push('invalid dependency');
    }
  }
  if (files.some(file => file.endsWith('.py')) && !files.includes(path.join(root, 'python/requirements.txt'))) errors.push('missing Python runtime requirements');
  for (const file of files) {
    const bytes = fs.readFileSync(file);
    if (bytes.includes(0)) continue;
    const text = bytes.toString('utf8');
    if (/\/Users\/|\/home\/[\w.-]+\/|[A-Za-z]:\\(?:Users|Documents and Settings)\\/.test(text)) errors.push(`local host path: ${path.relative(root, file)}`);
  }
  let externalLinks = 0;
  for (const file of files.filter(file => file.endsWith('.md'))) {
    const text = fs.readFileSync(file, 'utf8');
    const relative = path.relative(root, file);
    if (relative.startsWith('references' + path.sep) && text.replace(/\r?\n$/, '').split(/\r?\n/).length > 100) {
      const contents = /^## Contents\r?\n([\s\S]*?)(?=^## |$(?![\s\S]))/m.exec(text)?.[1] || '';
      const sections = [...text.matchAll(/^## (?!Contents\r?$)(.+)\r?$/gm)].map(m => anchor(m[1]));
      if (!sections.length || sections.some(section => !contents.includes(`](#${section})`))) errors.push(`missing navigation: ${relative}`);
    }
    for (const match of text.matchAll(/`((?:\.\.\/)?(?:references|assets|schemas|examples|python|bin)\/[^`\s<>]+)`/g)) {
      // Inline package paths use the package root unless explicitly ../ relative.
      const target = match[1];
      if (!/(?:\.[A-Za-z0-9]+|\/)$/.test(target) && target !== 'bin/archify') continue;
      const resolved = path.resolve(target.startsWith('../') ? path.dirname(file) : root, target);
      if (!resolved.startsWith(path.resolve(root) + path.sep) || !fs.existsSync(resolved)) errors.push(`missing inline resource: ${relative}: ${target}`);
    }
    for (const match of text.matchAll(/\[[^\]\n]+\]\(([^)\s]+)\)/g)) {
      const target = match[1];
      if (/^https?:/.test(target)) { externalLinks++; continue; }
      const [name, fragment] = target.split('#');
      const resolved = name ? path.resolve(path.dirname(file), name) : file;
      if (resolved !== root && !resolved.startsWith(path.resolve(root) + path.sep)) errors.push(`non-relative resource: ${relative}: ${target}`);
      else if (!fs.existsSync(resolved) || !fs.statSync(resolved).isFile()) errors.push(`missing resource: ${relative}: ${target}`);
      else if (fragment && resolved.endsWith('.md')) {
        const headings = [...fs.readFileSync(resolved, 'utf8').matchAll(/^#{1,6} (.+)$/gm)].map(m => anchor(m[1]));
        if (!headings.includes(fragment)) errors.push(`missing anchor: ${relative}: ${target}`);
      }
    }
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return { status: 'passed', files: files.length, externalLinks };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(checkSkillPackage(path.resolve(process.argv[2])))); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
