import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DIAGRAM_TYPES, DIAGRAM_TYPE_LABELS } from '../src/data/diagram-types.mjs';
import { PACKAGE_TYPES as VALIDATOR_PACKAGE_TYPES, validatePackage, validatePackageEntries } from '../src/data/community-packages.mjs';
import { publicGuideData, SCENARIO_RECIPES, startPromptsFor } from '../src/data/scenarios.mjs';

const websiteRoot = fileURLToPath(new URL('../', import.meta.url));
const repoRoot = path.resolve(websiteRoot, '..');
const read = relative => fs.readFileSync(path.join(websiteRoot, relative), 'utf8');

const recipeIds = [
  'system-overview', 'deployment-ownership', 'agent-tool-call', 'delivery-workflow',
  'incident-runbook', 'api-request', 'async-roundtrip', 'data-lineage', 'event-stream',
  'object-lifecycle', 'deployment-lifecycle', 'layout-repair',
];

test('website-local recipe data preserves its inventory, localized copy and starter prompt contract', () => {
  assert.deepEqual(SCENARIO_RECIPES.map(recipe => recipe.id), recipeIds);
  assert.deepEqual(DIAGRAM_TYPES, ['architecture', 'workflow', 'sequence', 'dataflow', 'lifecycle']);
  assert.deepEqual(DIAGRAM_TYPE_LABELS, {
    en: { architecture: 'Architecture', workflow: 'Workflow', sequence: 'Sequence', dataflow: 'Data flow', lifecycle: 'Lifecycle' },
    zh: { architecture: '架构图', workflow: '工作流', sequence: '时序图', dataflow: '数据流', lifecycle: '生命周期' },
  });

  const guide = publicGuideData();
  assert.deepEqual(guide.map(recipe => recipe.id), recipeIds);
  for (const recipe of SCENARIO_RECIPES) {
    const exposed = guide.find(candidate => candidate.id === recipe.id);
    assert.equal(exposed.en.prompt, recipe.en.prompt, `${recipe.id}: English prompt`);
    assert.equal(exposed.zh.prompt, recipe.zh.prompt, `${recipe.id}: Chinese prompt`);
    assert.deepEqual(exposed.en.include, recipe.en.include, `${recipe.id}: English evidence list`);
    assert.deepEqual(exposed.zh.include, recipe.zh.include, `${recipe.id}: Chinese evidence list`);
  }

  const starters = [
    ['architecture', 'system-overview'], ['workflow', 'agent-tool-call'],
    ['sequence', 'api-request'], ['dataflow', 'event-stream'],
    ['lifecycle', 'object-lifecycle'],
  ];
  for (const [type, id] of starters) {
    const recipe = SCENARIO_RECIPES.find(candidate => candidate.id === id);
    assert.equal(recipe?.type, type, `${type}: canonical starter recipe`);
    for (const lang of ['en', 'zh']) {
      const copy = recipe[lang];
      const repositoryPrompt = recipe.type === 'architecture'
        ? copy.prompt
        : lang === 'zh'
          ? `先检查这个仓库里的相关证据，然后${copy.prompt}不要编造代码无法支持的行为。`
          : `Inspect this repository for evidence, then ${copy.prompt.charAt(0).toLowerCase()}${copy.prompt.slice(1)} Do not invent behavior that the code does not support.`;
      assert.deepEqual(startPromptsFor(recipe, lang), {
        descriptionPrompt: recipe.start[lang].descriptionPrompt,
        repositoryPrompt,
      }, `${id}: ${lang} prompt compatibility`);
    }
    assert.deepEqual(startPromptsFor(recipe, 'fr'), startPromptsFor(recipe, 'en'), `${id}: unsupported language falls back to English`);
  }
});

test('website version and browser checks use existing local sources', () => {
  const releasePath = path.join(repoRoot, 'archify/skill-release.json');
  const release = JSON.parse(fs.readFileSync(releasePath, 'utf8'));
  assert.ok(release.version);
  assert.match(read('src/data/site.ts'), /import release from '\.\.\/\.\.\/\.\.\/archify\/skill-release\.json';/);
  assert.match(read('src/data/site.ts'), /export const version = release\.version;/);
  assert.match(read('src/data/product-facts.ts'), /import release from '\.\.\/\.\.\/\.\.\/archify\/skill-release\.json';/);
  assert.match(read('src/data/product-facts.ts'), /website\/src\/data\/scenarios\.mjs/);
  assert.match(read('test/migration.test.mjs'), /skill-release\.json/);
  assert.match(read('test/migration.test.mjs'), /const version = release\.version;/);

  const packageJson = JSON.parse(read('package.json'));
  const browserScript = packageJson.scripts['test:browser'];
  for (const target of ['test/site-language-continuity.test.mjs', 'test/community-browser.test.mjs']) {
    assert.ok(browserScript.includes(target), `browser gate includes ${target}`);
    assert.ok(fs.existsSync(path.join(websiteRoot, target)), `browser target exists: ${target}`);
  }
  assert.doesNotMatch(browserScript, /\.\.\/archify\/test\//);

  const fixture = read('test/helpers/community-fixture.mjs');
  assert.doesNotMatch(fixture, /scripts\/check-community-packages\.mjs/);
  assert.match(fixture, /'website\/src\/data\/community-packages\.mjs'/);

  for (const [file, source] of [
    ['src/data/site.ts', read('src/data/site.ts')],
    ['src/data/community.ts', read('src/data/community.ts')],
    ['src/data/community-packages.mjs', read('src/data/community-packages.mjs')],
    ['src/data/gallery-presentation.mjs', read('src/data/gallery-presentation.mjs')],
    ['src/pages/community.astro', read('src/pages/community.astro')],
    ['test/community-browser.test.mjs', read('test/community-browser.test.mjs')],
    ['test/helpers/community-fixture.mjs', read('test/helpers/community-fixture.mjs')],
    ['test/seo-content.test.mjs', read('test/seo-content.test.mjs')],
  ]) {
    assert.doesNotMatch(source, /(?:archify\/(?:package\.json|recipes\/scenarios\.mjs|bin\/visual-check\.mjs)|scripts\/site-copy\.mjs)/, `${file}: removed Node modules are not imported`);
  }
});

test('website-local community validator rejects invalid entries without the removed root script', () => {
  assert.deepEqual(VALIDATOR_PACKAGE_TYPES, ['skill', 'recipe', 'brand-marks', 'locale', 'wrapper']);
  const entry = {
    name: 'security-demo', type: 'skill',
    summary: { en: 'A sufficiently long English summary.', zh: '这是一个足够长的中文摘要' },
    author: { name: 'demo' }, repository: 'http://example.com/source',
    archify: '^3.0.1', schemaVersions: [1],
  };
  assert.deepEqual(validatePackage(entry), ['repository must be an https:// URL.']);
  const result = validatePackageEntries([['security-demo.json', entry]]);
  assert.equal(result.entries.length, 1);
  assert.deepEqual(result.failures, ['security-demo.json: repository must be an https:// URL.']);

  for (const [file, source] of [
    ['src/data/community.ts', read('src/data/community.ts')],
    ['src/data/community-packages.mjs', read('src/data/community-packages.mjs')],
    ['test/helpers/community-fixture.mjs', read('test/helpers/community-fixture.mjs')],
    ['test/source-decoupling.test.mjs', read('test/source-decoupling.test.mjs')],
  ]) {
    assert.doesNotMatch(source, /scripts\/check-community-packages\.mjs/, `${file}: no removed root validator reference`);
  }
});
