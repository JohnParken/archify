"""Opt-in real browser checks for comparison and pinned source evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[3]
@unittest.skipUnless(os.environ.get('ARCHIFY_BROWSER_TESTS')=='1','set ARCHIFY_BROWSER_TESTS=1 for real comparison and evidence browser checks')
class ComparisonEvidenceBrowserTests(unittest.TestCase):
    def test_comparison_views_guided_review_and_exports(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory(prefix='archify-comparison-browser-')as directory,sync_playwright()as p:
            output=Path(directory).resolve()/'delta.html'
            result=subprocess.run([sys.executable,str(ROOT/'archify/bin/archify.py'),'compare','architecture',str(ROOT/'archify/examples/checkout-platform.base.architecture.json'),str(ROOT/'archify/examples/checkout-platform.head.architecture.json'),str(output),'--browser','required','--json'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            browser=p.chromium.launch(headless=True);page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(output.as_uri())
            for view in ['base','delta','head']:
                page.locator('[data-tab="'+view+'"]').click();self.assertTrue(page.locator('[data-view="'+view+'"] svg').is_visible())
            page.locator('#review-next').click();self.assertGreater(page.locator('[data-review-active]').count(),0)
            exported=page.evaluate('Archify.deltaExport.canonicalSvg()');page.locator('#review-next').click();self.assertEqual(page.evaluate('Archify.deltaExport.canonicalSvg()'),exported)
            page.locator('#theme').click();self.assertEqual(page.locator('html').get_attribute('data-theme'),'light')
            for kind,magic in [('svg',b'<svg'),('png',b'\x89PNG\r\n\x1a\n')]:
                with page.expect_download()as download:page.locator('#export-'+kind).click()
                target=Path(directory)/('export.'+kind);download.value.save_as(target);self.assertTrue(target.read_bytes().startswith(magic))
            self.assertEqual(errors,[]);browser.close()
    def test_pinned_evidence_beacon_and_source_links_in_all_modes(self):
        from playwright.sync_api import sync_playwright
        git=os.environ.get('ARCHIFY_GIT') or shutil.which('git')
        self.assertTrue(git,'Git required for opted-in source evidence test')
        with tempfile.TemporaryDirectory(prefix='archify-evidence-browser-')as directory,sync_playwright()as p:
            folder=Path(directory).resolve();repo=folder/'repo';repo.mkdir();(repo/'source.py').write_text('one\ntwo\nthree\n')
            def command(*args):return subprocess.check_output([git,'-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
            for args in [('init','-q'),('config','user.name','Tests'),('config','user.email','test@example.test'),('remote','add','origin','git@github.com:example/project.git'),('add','.'),('commit','-qm','pinned')]:command(*args)
            revision=command('rev-parse','HEAD');(repo/'source.py').write_text('dirty\n')
            browser=p.chromium.launch(headless=True)
            examples={'architecture':('web-app.architecture.json','components'),'workflow':('agent-tool-call.workflow.json','nodes'),'sequence':('cache-miss-request.sequence.json','participants'),'dataflow':('product-analytics.dataflow.json','nodes'),'lifecycle':('deployment-release.lifecycle.json','states')}
            for mode,(example,collection)in examples.items():
                with self.subTest(mode=mode):
                    doc=json.loads((ROOT/'archify/examples'/example).read_text());doc['meta']['repository']={'url':'https://github.com/example/project','revision':revision};node=doc[collection][0];node['sources']=[{'path':'source.py','line':1,'end_line':3,'label':'Source </script> safe'}]
                    source=folder/(mode+'.json');source.write_text(json.dumps(doc));output=folder/(mode+'.html')
                    result=subprocess.run([sys.executable,str(ROOT/'archify/bin/archify.py'),'finalize',mode,str(source),str(output),'--repo-root',str(repo),'--json'],capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(output.as_uri());self.assertGreater(page.locator('[data-source-evidence-beacon]').count(),0)
                    page.locator('[data-node-id="'+node['id']+'"]').first.click();self.assertGreater(page.locator('a[href*="/blob/'+revision+'/"]').count(),0);self.assertEqual(errors,[]);page.close()
            browser.close()
