import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { ChromeVisualBrowser, findChrome } from './helpers/chrome-browser.mjs';
import { DIAGRAM_TYPES, DIAGRAM_TYPE_LABELS } from '../src/data/diagram-types.mjs';
import { CASES } from '../src/data/gallery-presentation.mjs';
import { SCENARIO_RECIPES } from '../src/data/scenarios.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '../..');
const integrationEnabled = process.env.ARCHIFY_SITE_INTEGRATION === '1';
const chromePath = integrationEnabled && process.env.ARCHIFY_CHROME ? findChrome() : null;

async function evaluate(browser, sessionId, expression) {
  const response = await browser.cdp.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, sessionId);
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.exception?.description
      || response.exceptionDetails.text
      || 'Runtime.evaluate failed');
  }
  return response.result?.value;
}

async function navigate(browser, sessionId, url, expectedUrl = url) {
  const loaded = browser.cdp.waitFor('Page.loadEventFired', sessionId);
  loaded.catch(() => {}); // Keep early rejection handled; the mandatory await below still fails.
  const navigation = await browser.cdp.send('Page.navigate', { url }, sessionId);
  if (navigation.errorText) throw new Error(`Chrome navigation failed: ${navigation.errorText}`);
  try { await loaded; }
  catch (error) { throw new Error(`Page load failed while navigating to ${url} (expected ${expectedUrl}): ${error.message}`, { cause: error }); }
  if (expectedUrl !== url) {
    for (let attempt = 0; attempt < 100; attempt++) {
      try {
        const settled = await evaluate(browser, sessionId, `location.href === ${JSON.stringify(expectedUrl)} && document.readyState === 'complete' && !!document.querySelector('.site-nav')`);
        if (settled) return;
      } catch (_) { /* A compatibility redirect destroys the previous execution context. */ }
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    const actual = await evaluate(browser, sessionId, '({ href: location.href, readyState: document.readyState })');
    assert.fail(`Compatibility navigation did not settle at ${expectedUrl}; actual ${JSON.stringify(actual)}`);
  }
}

async function clickAndNavigate(browser, sessionId, selector) {
  const context = await evaluate(browser, sessionId, `({ from: location.href, to: document.querySelector(${JSON.stringify(selector)})?.href || null })`);
  const loaded = browser.cdp.waitFor('Page.loadEventFired', sessionId);
  loaded.catch(() => {});
  try {
    await evaluate(browser, sessionId, `(function () {
      var link = document.querySelector(${JSON.stringify(selector)});
      if (!link) throw new Error('Missing navigation link: ' + ${JSON.stringify(selector)});
      link.click();
    })()`);
    await loaded;
  } catch (error) {
    throw new Error(`Navigation click ${selector} from ${context.from} to ${context.to} failed: ${error.message}`, { cause: error });
  }
}

function startStaticServer(root, basePath = '') {
  const server = http.createServer((request, response) => {
    const requestUrl = new URL(request.url || '/', 'http://127.0.0.1');
    if (basePath && !requestUrl.pathname.startsWith(`${basePath}/`)) {
      response.writeHead(404).end('Not found');
      return;
    }
    let relative;
    try { relative = decodeURIComponent(requestUrl.pathname.slice(basePath.length)).replace(/^\/+/, '') || 'index.html'; }
    catch (_) { response.writeHead(400).end('Bad request'); return; }
    // Only the ten published pages have extensionless aliases; unknown routes stay 404.
    if (process.env.ARCHIFY_SITE_ROOT && /^(?:zh|(?:zh\/)?(?:guide|start|gallery|community))$/.test(relative)) relative += '.html';
    const requestedPath = path.resolve(root, relative);
    if (!requestedPath.startsWith(`${path.resolve(root)}${path.sep}`)) {
      response.writeHead(403).end('Forbidden');
      return;
    }
    try {
      const body = fs.readFileSync(requestedPath);
      const contentType = requestedPath.endsWith('.css') ? 'text/css'
        : requestedPath.endsWith('.js') ? 'text/javascript'
          : requestedPath.endsWith('.svg') ? 'image/svg+xml'
          : requestedPath.endsWith('.png') ? 'image/png'
          : requestedPath.endsWith('.json') ? 'application/json'
            : 'text/html';
      response.writeHead(200, { 'content-type': `${contentType}; charset=utf-8` });
      response.end(body);
    } catch (_) {
      response.writeHead(404).end('Not found');
    }
  });
  return server;
}
test('real Chrome preserves language through entry, navigation, selection, refresh, and consistent navigation chrome', {
  skip: chromePath ? false : 'Set ARCHIFY_CHROME to run the real site regression.',
  timeout: 120000,
}, async () => {
  const builtSite = !!process.env.ARCHIFY_SITE_ROOT;
  const chineseLang = builtSite ? 'zh-Hans' : 'zh-CN';
  const docsRoot = process.env.ARCHIFY_SITE_ROOT ? path.resolve(process.env.ARCHIFY_SITE_ROOT) : path.join(repoRoot, 'docs');
  const basePath = process.env.ARCHIFY_SITE_ROOT ? (process.env.ARCHIFY_SITE_BASE ?? '/archify') : '';
  const server = startStaticServer(docsRoot, basePath);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}${basePath}`;
  const browser = new ChromeVisualBrowser(chromePath);
  const cloudflare = builtSite && basePath === '';
  // Expectations are independent of the production URL helper.
  const pageHref = (page, language = 'en') => {
    if (!builtSite) return page;
    if (page === 'index.html') return language === 'zh' ? (cloudflare ? 'zh' : 'zh.html') : './';
    return (language === 'zh' ? 'zh/' : '') + (cloudflare ? page.replace(/\.html$/, '') : page);
  };
  const pageUrl = (page, language = 'en', suffix = '') => new URL(pageHref(page, language) + suffix, `${baseUrl}/`).href;
  const navSelector = (page, language) => `.site-nav a[href="${pageHref(page, language)}"]`;
  async function assertBuiltNavigation(sessionId, page, language, rootUrl = `${baseUrl}/`) {
    const receipt = await evaluate(browser, sessionId, `({
      base: document.querySelector('base').getAttribute('href'),
      resolvedBase: document.baseURI,
      links: [...document.querySelectorAll('.site-nav .nav-link')].filter(link => !link.href.startsWith('https://github.com/')).map(link => ({ raw: link.getAttribute('href'), resolved: link.href })),
      assets: [...document.querySelectorAll('.nav-logo img[src^="assets/"]')].map(image => ({ url: image.src, loaded: image.complete && image.naturalWidth > 0 })),
      icon: document.querySelector('link[rel="icon"]').href,
      logo: document.querySelector('.site-nav .nav-logo').href,
      emphasis: [...document.querySelectorAll('h1 em')].map(node => ({
        style: getComputedStyle(node).fontStyle, weight: getComputedStyle(node).fontWeight,
        family: getComputedStyle(node).fontFamily, headingFamily: getComputedStyle(node.closest('h1')).fontFamily
      }))
    })`);
    if (page !== 'start.html') {
      assert.ok(receipt.emphasis.length > 0, `${page}: heading emphasis exists`);
      for (const emphasis of receipt.emphasis) {
        assert.equal(emphasis.style, language === 'zh' ? 'normal' : 'italic', `${page} ${language}: localized heading emphasis style`);
        assert.equal(emphasis.weight, language === 'zh' ? '600' : '400', `${page} ${language}: localized heading emphasis weight`);
        if (language === 'zh') {
          assert.equal(emphasis.family, emphasis.headingFamily, `${page}: Chinese emphasis inherits its heading family`);
          assert.match(emphasis.family, /(?:^|,)\s*sans-serif\s*$/, `${page}: Chinese heading uses the portable sans-serif stack`);
        }
      }
    }
    assert.equal(receipt.base, language === 'zh' && page !== 'index.html' ? '../' : './', `${page}: relative export base`);
    assert.equal(receipt.resolvedBase, rootUrl, `${page}: resolved resource base`);
    assert.deepEqual(receipt.links, ['guide.html', 'gallery.html', 'start.html', 'community.html'].map(target => ({
      raw: pageHref(target, language), resolved: new URL(pageHref(target, language), rootUrl).href,
    })), `${page} ${language}: exact resolved navigation routes`);
    assert.equal(receipt.logo, new URL(pageHref('index.html', language) + (page === 'index.html' ? '#' : ''), rootUrl).href, `${page}: localized home link`);
    assert.equal(receipt.icon, new URL('assets/archify-mark.svg', rootUrl).href, `${page}: favicon uses resource base`);
    assert.ok(receipt.assets.length > 0, `${page}: brand resource exists`);
    for (const asset of receipt.assets) {
      assert.ok(asset.url.startsWith(new URL('assets/', rootUrl).href), `${page}: resource URL belongs to root assets`);
      assert.ok(asset.loaded, `${page}: brand image loaded`);
    }
  }

  try {
    const sessionId = await browser.sessionPromise;
    if (builtSite) {
      for (const unknown of ['/missing-page', '/zh/missing-page', '/zh/guide/extra']) {
        assert.equal((await fetch(`${baseUrl}${unknown}`)).status, 404, `${unknown}: unknown routes never receive a page fallback`);
      }
      assert.equal((await fetch(`${baseUrl}/%2e%2e%2foutside.html`)).status, 403, 'encoded path traversal is blocked');
    }
    await browser.cdp.send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    }, sessionId);

    if (builtSite) {
      const staticContent = {
        'index.html': ['.hero h1', '.hero h1 .l', '.hero-sub', '.fade-up', '.product-facts h2', '.product-facts article h3', '.product-facts article p'],
        'gallery.html': ['.hero h1', '.hero-copy', '.showcase-card', '.showcase-card .card-title', '.showcase-card .card-description'],
        'community.html': ['.hero h1', '.hero-copy', '.package-card', '.package-name', '.package-summary'],
      };
      await browser.cdp.send('Emulation.setScriptExecutionDisabled', { value: true }, sessionId);
      try {
        for (const language of ['en', 'zh']) for (const [page, selectors] of Object.entries(staticContent)) {
          await navigate(browser, sessionId, pageUrl(page, language));
          // CSS entrance animations can still be in progress at Page.loadEventFired.
          // Wait for their actual completion; decorative infinite animations are unrelated.
          await evaluate(browser, sessionId, `(async function () {
            var animations = document.getAnimations().filter(function (animation) {
              return animation.effect && Number.isFinite(animation.effect.getComputedTiming().endTime);
            });
            var deadline;
            try {
              await Promise.race([
                Promise.all(animations.map(function (animation) { return animation.finished; })),
                new Promise(function (_, reject) {
                  deadline = setTimeout(function () { reject(new Error('Finite CSS animations did not finish within 5 seconds')); }, 5000);
                })
              ]);
            } finally { clearTimeout(deadline); }
          })()`);
          const receipt = await evaluate(browser, sessionId, `(function () {
            return {
              language: document.documentElement.lang,
              runtimeAbsent: typeof window.ArchifySiteLanguage === 'undefined',
              enhancedNodes: document.querySelectorAll('.fade-up.visible, .showcase-card.visible, .package-card.visible').length,
              groups: ${JSON.stringify(selectors)}.map(function (selector) {
                return { selector, nodes: [...document.querySelectorAll(selector)].map(function (node) {
                  var style = getComputedStyle(node), box = node.getBoundingClientRect();
                  var ancestorsVisible = true;
                  for (var ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
                    var parentStyle = getComputedStyle(ancestor);
                    if (Number(parentStyle.opacity) !== 1 || parentStyle.display === 'none' || parentStyle.visibility !== 'visible') ancestorsVisible = false;
                  }
                  return { text: node.textContent.trim().length, opacity: Number(style.opacity), display: style.display, visibility: style.visibility, width: box.width, height: box.height, ancestorsVisible };
                }) };
              })
            };
          })()`);
          assert.equal(receipt.language, language === 'zh' ? chineseLang : 'en', `${page} ${language}: static language`);
          assert.ok(receipt.runtimeAbsent, `${page} ${language}: site JavaScript must remain disabled`);
          assert.equal(receipt.enhancedNodes, 0, `${page} ${language}: visibility cannot depend on adding .visible`);
          for (const group of receipt.groups) {
            assert.ok(group.nodes.length > 0, `${page} ${language}: static ${group.selector} exists`);
            for (const node of group.nodes) {
              const label = `${page} ${language} ${group.selector}: static content is visible without JavaScript`;
              assert.ok(node.text > 0, label);
              assert.equal(node.opacity, 1, label);
              assert.notEqual(node.display, 'none', label);
              assert.equal(node.visibility, 'visible', label);
              assert.ok(node.width > 0 && node.height > 0 && node.ancestorsVisible, label);
            }
          }
        }
      } finally {
        await browser.cdp.send('Emulation.setScriptExecutionDisabled', { value: false }, sessionId);
      }
    }

    await navigate(browser, sessionId, `${baseUrl}/index.html`);
    await evaluate(browser, sessionId, 'localStorage.clear()');

    await evaluate(browser, sessionId, "localStorage.setItem('archify-guide-language', 'zh')");
    await navigate(browser, sessionId, `${baseUrl}/index.html`);
    if (builtSite) {
      assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en', 'English URL wins over legacy storage');
      await evaluate(browser, sessionId, "localStorage.setItem('archify-lang', 'zh')");
      await navigate(browser, sessionId, pageUrl('index.html'));
      assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en', 'English URL wins over canonical storage');
      await evaluate(browser, sessionId, 'localStorage.clear()');
      await navigate(browser, sessionId, pageUrl('index.html', 'zh'));
      assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), chineseLang, 'Bare Chinese URL is Chinese without storage');
      assert.equal(await evaluate(browser, sessionId, 'document.querySelector("link[rel=canonical]").href'), 'https://archify.si/zh');
    } else {
      assert.deepEqual(await evaluate(browser, sessionId, `({
        language: document.documentElement.lang,
        stored: localStorage.getItem('archify-lang')
      })`), { language: chineseLang, stored: 'zh' });
    }

    await evaluate(browser, sessionId, 'localStorage.clear()');
    await navigate(browser, sessionId, `${baseUrl}/index.html?lang=zh&utm_source=browser-test#proof`, builtSite ? pageUrl('index.html', 'zh', '?utm_source=browser-test#proof') : undefined);

    let state = await evaluate(browser, sessionId, `({
      language: document.documentElement.lang,
      stored: localStorage.getItem('archify-lang'),
      langQuery: new URL(location.href).searchParams.get('lang'),
      campaign: new URL(location.href).searchParams.get('utm_source'),
      hash: location.hash
    })`);
    assert.deepEqual(state, {
      language: chineseLang, stored: 'zh', langQuery: null, campaign: 'browser-test', hash: '#proof',
    });

    await clickAndNavigate(browser, sessionId, navSelector('gallery.html', 'zh'));
    assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), chineseLang);
    assert.equal(await evaluate(browser, sessionId, 'document.querySelector(".nav-logo-path").textContent'), '/ 验证作品集');
    assert.deepEqual(await evaluate(browser, sessionId, `Array.from(document.querySelectorAll('[data-filter]')).map(function (button) {
      return button.textContent;
    })`), [`全部配方 / ${CASES.length}`, ...DIAGRAM_TYPES.map(type => DIAGRAM_TYPE_LABELS.zh[type])]);

    await evaluate(browser, sessionId, 'document.querySelector(\'[data-filter="architecture"]\').click()');
    state = await evaluate(browser, sessionId, `({
      language: document.documentElement.lang,
      selected: document.querySelector('[data-filter="architecture"]').getAttribute('aria-pressed'),
      typeQuery: new URL(location.href).searchParams.get('type'),
      visibleCount: document.querySelectorAll('.showcase-card:not([hidden])').length,
      onlyArchitecture: Array.from(document.querySelectorAll('.showcase-card:not([hidden])')).every(function (card) {
        return card.getAttribute('data-type') === 'architecture';
      })
    })`);
    assert.deepEqual(state, {
      language: chineseLang, selected: 'true', typeQuery: 'architecture', visibleCount: 2, onlyArchitecture: true,
    });

    let loaded = browser.cdp.waitFor('Page.loadEventFired', sessionId);
    await browser.cdp.send('Page.reload', {}, sessionId);
    await loaded;
    assert.deepEqual(await evaluate(browser, sessionId, `({
      language: document.documentElement.lang,
      selected: document.querySelector('[data-filter="architecture"]').getAttribute('aria-pressed'),
      visibleCount: document.querySelectorAll('.showcase-card:not([hidden])').length
    })`), { language: chineseLang, selected: 'true', visibleCount: 2 });

    if (builtSite) {
      await evaluate(browser, sessionId, "history.replaceState(null, '', location.pathname + '?type=architecture&utm_source=switch#proof')");
      assert.equal(await evaluate(browser, sessionId, 'document.querySelector("[data-language-switch]").tagName'), 'A');
      await clickAndNavigate(browser, sessionId, '[data-language-switch]');
      assert.equal(await evaluate(browser, sessionId, 'location.href'), pageUrl('gallery.html', 'en', '?type=architecture&utm_source=switch#proof'));
      assert.equal(await evaluate(browser, sessionId, `document.querySelector('[data-filter="architecture"]').getAttribute('aria-pressed')`), 'true');
      await clickAndNavigate(browser, sessionId, '[data-language-switch]');
      assert.equal(await evaluate(browser, sessionId, 'location.href'), pageUrl('gallery.html', 'zh', '?type=architecture&utm_source=switch#proof'));
      assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), chineseLang);
      await clickAndNavigate(browser, sessionId, '[data-language-switch]');
      assert.equal(await evaluate(browser, sessionId, 'location.href'), pageUrl('gallery.html', 'en', '?type=architecture&utm_source=switch#proof'));
    } else {
      await evaluate(browser, sessionId, 'document.getElementById("language").click()');
    }
    assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en');
    assert.equal(await evaluate(browser, sessionId, 'document.querySelector(".nav-logo-path").textContent'), '/ proof lab');
    assert.deepEqual(await evaluate(browser, sessionId, `Array.from(document.querySelectorAll('[data-filter]')).map(function (button) {
      return button.textContent;
    })`), [`All / ${CASES.length}`, ...DIAGRAM_TYPES.map(type => DIAGRAM_TYPE_LABELS.en[type])]);

    loaded = browser.cdp.waitFor('Page.loadEventFired', sessionId);
    await browser.cdp.send('Page.reload', {}, sessionId);
    await loaded;
    assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en');

    await clickAndNavigate(browser, sessionId, navSelector('guide.html', 'en'));
    assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en');
    await navigate(browser, sessionId, `${baseUrl}/guide.html?lang=zh#recipes`, builtSite ? pageUrl('guide.html', 'zh', '#recipes') : undefined);
    state = await evaluate(browser, sessionId, `({
      language: document.documentElement.lang,
      stored: localStorage.getItem('archify-lang'),
      langQuery: new URL(location.href).searchParams.get('lang'),
      hash: location.hash
    })`);
    assert.deepEqual(state, { language: chineseLang, stored: 'zh', langQuery: null, hash: '#recipes' });
    assert.equal(await evaluate(browser, sessionId, 'document.querySelector(".nav-logo-path").textContent'), '/ 场景指南');
    assert.deepEqual(await evaluate(browser, sessionId, `Array.from(document.querySelectorAll('#filters [data-filter]')).map(function (button) {
      return button.textContent;
    })`), ['全部配方', ...DIAGRAM_TYPES.map(type => DIAGRAM_TYPE_LABELS.zh[type])]);

    await evaluate(browser, sessionId, 'document.querySelector(\'#filters [data-filter="sequence"]\').click()');
    state = await evaluate(browser, sessionId, `({
      language: document.documentElement.lang,
      selected: document.querySelector('#filters [data-filter="sequence"]').classList.contains('active'),
      visibleCount: document.querySelectorAll('#cards .card').length,
      onlySequence: Array.from(document.querySelectorAll('#cards .card .card-type')).every(function (label) {
        return label.textContent === 'sequence';
      }),
      labels: Array.from(document.querySelectorAll('#filters [data-filter]')).map(function (button) {
        return button.textContent;
      })
    })`);
    assert.deepEqual(state, {
      language: chineseLang,
      selected: true,
      visibleCount: 2,
      onlySequence: true,
      labels: ['全部配方', ...DIAGRAM_TYPES.map(type => DIAGRAM_TYPE_LABELS.zh[type])],
    });

    if (builtSite) {
      const question = 'Guide draft continuity: approval workflow with reviewer handoff 审核交接';
      const suffix = '?utm_source=guide-state&type=workflow#recipes';
      await navigate(browser, sessionId, pageUrl('guide.html', 'en'));
      // A filter or campaign can change the URL after the switch link first initializes.
      await evaluate(browser, sessionId, `history.replaceState(null, '', location.pathname + ${JSON.stringify(suffix)})`);
      const workflowRecipes = SCENARIO_RECIPES.filter(recipe => recipe.type === 'workflow');
      assert.ok(workflowRecipes.length > 0, 'canonical inventory includes workflow recipes');
      const selectedRecipe = { id: workflowRecipes[0].id, en: workflowRecipes[0].en.title, zh: workflowRecipes[0].zh.title };
      assert.equal(await evaluate(browser, sessionId, `(function () {
        document.getElementById('scenario').value = ${JSON.stringify(question)};
        document.querySelector('#filters [data-filter="workflow"]').click();
        var card = document.querySelector('#cards .card[data-recipe]');
        card.click();
        return card.dataset.recipe;
      })()`), selectedRecipe.id, 'first workflow card matches the canonical recipe inventory');
      for (const language of ['zh', 'en']) {
        await clickAndNavigate(browser, sessionId, '[data-language-switch]');
        assert.equal(await evaluate(browser, sessionId, 'location.href'), pageUrl('guide.html', language, suffix));
        assert.deepEqual(await evaluate(browser, sessionId, `({
          language: document.documentElement.lang,
          scenario: document.getElementById('scenario').value,
          filter: document.querySelector('#filters .filter.active').dataset.filter,
          visibleCount: document.querySelectorAll('#cards .card').length,
          onlyWorkflow: [...document.querySelectorAll('#cards .card-type')].every(node => node.textContent === 'workflow'),
          selectedCardExists: !!document.querySelector('#cards .card[data-recipe="${selectedRecipe.id}"]'),
          resultVisible: document.getElementById('result').classList.contains('visible'),
          title: document.querySelector('#result h3').textContent,
          pendingState: sessionStorage.getItem('archify-guide-language-state.v1')
        })`), {
          language: language === 'zh' ? chineseLang : 'en', scenario: question, filter: 'workflow',
          visibleCount: workflowRecipes.length, onlyWorkflow: true, selectedCardExists: true, resultVisible: true,
          title: selectedRecipe[language], pendingState: null,
        }, `${language}: Guide language navigation preserves the draft, filter and selected recipe and consumes temporary state`);
        assert.equal(await evaluate(browser, sessionId, 'new URL(location.href).searchParams.has("scenario")'), false, 'Guide draft stays out of the URL');
      }
      await navigate(browser, sessionId, pageUrl('guide.html', 'zh', '#recipes'));
    }

    await clickAndNavigate(browser, sessionId, navSelector('start.html', 'zh'));
    assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), chineseLang);
    assert.equal(await evaluate(browser, sessionId, 'new URL(location.href).searchParams.has("lang")'), false);
    assert.equal(await evaluate(browser, sessionId, 'document.querySelector(".nav-logo-path").textContent'), '/ 快速上手');
    if (builtSite) {
      await navigate(browser, sessionId, pageUrl('start.html', 'zh', '?lang=en&utm_source=legacy#install'), pageUrl('start.html', 'en', '?utm_source=legacy&type=architecture&agent=codex&source=direct&input=description#install'));
      assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), 'en');
      assert.equal(await evaluate(browser, sessionId, 'localStorage.getItem("archify-lang")'), 'en');
    }

    const pages = ['index.html', 'gallery.html', 'guide.html', 'start.html'];
    if (fs.existsSync(path.join(docsRoot, 'community.html'))) pages.push('community.html');
    if (process.env.ARCHIFY_SITE_ROOT) assert.ok(pages.includes('community.html'), 'built site must include the catalog');
    const desktopReceipts = [];
    for (const page of pages) {
      await navigate(browser, sessionId, builtSite ? pageUrl(page, 'zh') : `${baseUrl}/${page}`);
      if (builtSite) await assertBuiltNavigation(sessionId, page, 'zh');
      desktopReceipts.push(await evaluate(browser, sessionId, `(function () {
        var nav = document.querySelector('.site-nav');
        var logo = nav.querySelector('.nav-logo-text');
        var actions = nav.querySelector('.nav-right');
        var language = nav.querySelector('.btn-lang');
        var cta = nav.querySelector('.nav-cta');
        var navStyle = getComputedStyle(nav);
        var logoStyle = getComputedStyle(logo);
        var actionsStyle = getComputedStyle(actions);
        var languageStyle = getComputedStyle(language);
        var ctaStyle = getComputedStyle(cta);
        return {
          height: nav.getBoundingClientRect().height,
          position: navStyle.position,
          paddingLeft: navStyle.paddingLeft,
          background: navStyle.backgroundColor,
          borderBottom: navStyle.borderBottomWidth + ' ' + navStyle.borderBottomStyle + ' ' + navStyle.borderBottomColor,
          logoFont: logoStyle.fontFamily,
          logoSize: logoStyle.fontSize,
          actionGap: actionsStyle.gap,
          languageHeight: language.getBoundingClientRect().height,
          languageRadius: languageStyle.borderRadius,
          ctaHeight: cta.getBoundingClientRect().height,
          ctaRadius: ctaStyle.borderRadius,
          linkCount: nav.querySelectorAll('.nav-link').length,
          communityLabel: nav.querySelector(${JSON.stringify('a[href="' + pageHref('community.html', 'zh') + '"]')}).textContent.trim()
        };
      })()`));
    }
    assert.equal(desktopReceipts[0].linkCount, 5);
    assert.equal(desktopReceipts[0].communityLabel, '社区包');
    for (const receipt of desktopReceipts.slice(1)) assert.deepEqual(receipt, desktopReceipts[0]);

    for (const width of [320, 390, 768]) {
      await browser.cdp.send('Emulation.setDeviceMetricsOverride', {
        width, height: 844, deviceScaleFactor: 1, mobile: true,
      }, sessionId);
      for (const language of ['en', 'zh']) {
        for (const page of pages) {
          await navigate(browser, sessionId, builtSite ? pageUrl(page, language) : `${baseUrl}/${page}?lang=${language}`);
          if (builtSite) await assertBuiltNavigation(sessionId, page, language);
          const mobile = await evaluate(browser, sessionId, `(function () {
            var nav = document.querySelector('.site-nav');
            var rect = nav.getBoundingClientRect();
            var community = nav.querySelector(${JSON.stringify('a[href="' + pageHref('community.html', language) + '"]')});
            var linkRect = community.getBoundingClientRect();
            var x = linkRect.x + linkRect.width / 2;
            var y = linkRect.y + linkRect.height / 2;
            return {
              height: rect.height, left: rect.left, right: rect.right, pageWidth: document.documentElement.scrollWidth,
              communityLabel: community.textContent.trim(),
              language: document.documentElement.lang,
              active: nav.querySelector('[aria-current="page"]')?.getAttribute('href') || null,
              linksFit: [...nav.querySelectorAll('a, button')].every(node => {
                var box = node.getBoundingClientRect();
                return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= rect.bottom;
              }),
              reachable: document.elementFromPoint(x, y)?.closest('a') === community,
              targetHeight: linkRect.height, x, y
            };
          })()`);
          assert.equal(mobile.height, 104, page);
          assert.equal(mobile.left, 0, page);
          assert.equal(mobile.right, width, page);
          assert.ok(mobile.pageWidth <= width, `${page} ${language} ${width}: page must not overflow`);
          assert.equal(mobile.language, language === 'zh' ? chineseLang : 'en', page);
          assert.equal(mobile.communityLabel, language === 'zh' ? '社区包' : 'Community', page);
          assert.equal(mobile.active, page === 'index.html' ? null : pageHref(page, language), page);
          assert.ok(mobile.linksFit, `${page} ${language} ${width}: navigation must fit`);
          assert.ok(mobile.reachable, `${page} ${language} ${width}: Community must be reachable`);
          assert.ok(mobile.targetHeight >= 44, 'mobile navigation must retain a 44px touch target');
          if (page === 'index.html' && pages.includes('community.html')) {
            const loaded = browser.cdp.waitFor('Page.loadEventFired', sessionId);
            for (const type of ['mousePressed', 'mouseReleased']) {
              await browser.cdp.send('Input.dispatchMouseEvent', { type, x: mobile.x, y: mobile.y, button: 'left', clickCount: 1 }, sessionId);
            }
            await loaded;
            assert.equal(await evaluate(browser, sessionId, 'location.pathname'), new URL(pageUrl('community.html', language)).pathname);
            assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), language === 'zh' ? chineseLang : 'en');
            assert.equal(await evaluate(browser, sessionId, 'document.querySelector(".site-nav a[aria-current=page]").getAttribute("href")'), pageHref('community.html', language));
          }
        }
      }
    }
    if (builtSite && !cloudflare) {
      // Exported GitHub HTML must resolve the same navigation and CSS on file://.
      const fileRoot = pathToFileURL(`${docsRoot}${path.sep}`).href;
      for (const language of ['en', 'zh']) {
        for (const page of pages) {
          const exported = page === 'index.html' && language === 'en' ? 'index.html' : pageHref(page, language);
          await navigate(browser, sessionId, new URL(exported, fileRoot).href);
          assert.equal(await evaluate(browser, sessionId, 'document.documentElement.lang'), language === 'zh' ? chineseLang : 'en');
          await assertBuiltNavigation(sessionId, page, language, fileRoot);
        }
      }
    }
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
});
