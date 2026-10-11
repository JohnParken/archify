"""Compatibility command forms and independent output-path preflights."""
from contextlib import redirect_stdout
from io import StringIO
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from archify_runtime.cli import main
ROOT=Path(__file__).resolve().parents[2]
class CommandCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='archify-commands-');self.root=Path(self.temp.name).resolve()
        self.source=self.root/'candidate.json';self.source.write_bytes((ROOT/'examples/web-app.architecture.json').read_bytes());self.output=self.root/'diagram.html'
    def tearDown(self):self.temp.cleanup()
    def invoke(self,*args):
        text=StringIO()
        with redirect_stdout(text):status=main([*map(str,args),'--json'])
        return status,json.loads(text.getvalue())
    def invoke_ordered(self,*args):
        text=StringIO()
        with redirect_stdout(text):status=main(list(map(str,args)))
        return status,json.loads(text.getvalue())
    def test_output_only_native_and_bare_html_checks(self):
        self.assertEqual(self.invoke('finalize','architecture',self.source,self.output)[0],0)
        self.assertEqual(self.invoke('check',self.output,'--require-provenance')[0],0)
        bare=self.root/'bare.html';bare.write_bytes(self.output.read_bytes())
        status,result=self.invoke('check',bare);self.assertEqual(status,0);self.assertEqual(result['provenance'],'unavailable')
        self.assertEqual(self.invoke('check',bare,'--require-provenance')[0],1)
    def test_candidate_hash_and_reserved_receipt_paths_preserve_previous_output(self):
        self.assertEqual(self.invoke('finalize','architecture',self.source,self.output)[0],0);previous=self.output.read_bytes()
        for options in [('--candidate-sha256','0'*64),('--receipt',self.output.with_suffix('.delivery-pending.json')),('--receipt',self.root/'.archify-delivery-lock.json')]:
            status,result=self.invoke('finalize','architecture',self.source,self.output,*options)
            self.assertNotEqual(status,0,result);self.assertEqual(self.output.read_bytes(),previous)
    def test_portable_explicit_names_and_relative_dot_syntax(self):
        current=Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(self.invoke('finalize','architecture',self.source,'./nested/good.html')[0],0)
            for name in ['CON.html','bad:stream.html','bad?.html','bad.html/']:
                self.assertNotEqual(self.invoke('finalize','architecture',self.source,name)[0],0,name)
        finally:os.chdir(current)
    def test_advanced_usage_errors_are_json_and_exit_two(self):
        status,result=self.invoke('compare','architecture',self.source)
        self.assertEqual(status,2);self.assertFalse(result['ok']);self.assertEqual(result['diagnostics'][0]['code'],'cli/invalid-arguments')

    def test_html_and_typed_check_accept_interspersed_options(self):
        self.assertEqual(self.invoke('finalize','architecture',self.source,self.output,
                                     '--quality','standard')[0],0)
        calls = [
            ['--json','check',self.output,'--require-provenance'],
            ['check','--require-provenance',self.output,'--json'],
            ['check',self.output,'--json','--require-provenance'],
        ]
        for args in calls:
            with self.subTest(args=args):
                status,result=self.invoke_ordered(*args)
                self.assertEqual(status,0,result)

        calls = [
            ['--json','check','architecture',self.source,self.output,'--layout-json'],
            ['check','--quality','standard','architecture',self.source,self.output,
             '--layout-json','--json'],
        ]
        for args in calls:
            with self.subTest(args=args):
                status,result=self.invoke_ordered(*args)
                self.assertEqual(status,0,result)

    def test_compare_candidate_hash_binds_head_and_output_check_input(self):
        base=self.root/'base.json';head=self.root/'head.json';output=self.root/'delta.html'
        base.write_bytes((ROOT/'examples/checkout-platform.base.architecture.json').read_bytes())
        head.write_bytes((ROOT/'examples/checkout-platform.head.architecture.json').read_bytes())
        expected=hashlib.sha256(head.read_bytes()).hexdigest()
        status,result=self.invoke('compare','architecture',base,head,output,
                                  '--candidate-sha256',expected)
        self.assertEqual(status,0,result)
        previous=output.read_bytes()

        status,result=self.invoke('check',output,'--candidate-sha256',expected)
        self.assertEqual(status,0,result)
        status,result=self.invoke('check',output,'--candidate-sha256','0'*64)
        self.assertEqual(status,1,result)
        self.assertEqual(result['stage'],'check')
        self.assertEqual(result['diagnostics'][0]['code'],'integrity/specification-mismatch')
        self.assertEqual(output.read_bytes(),previous)

        status,result=self.invoke('compare','architecture',base,head,output,
                                  '--candidate-sha256','0'*64)
        self.assertEqual(status,1,result)
        self.assertEqual(result['diagnostics'][0]['code'],'integrity/specification-mismatch')
        self.assertEqual(output.read_bytes(),previous)
        status,result=self.invoke('compare','architecture',base,head,output,
                                  '--candidate-sha256','invalid')
        self.assertEqual(status,2,result)
        self.assertEqual(result['diagnostics'][0]['code'],'cli/invalid-arguments')

        finalized=self.root/'finalized.html'
        self.assertEqual(self.invoke('finalize','architecture',self.source,finalized)[0],0)
        bare=self.root/'bare.html';bare.write_bytes(finalized.read_bytes())
        status,result=self.invoke('check',bare,'--candidate-sha256',expected)
        self.assertEqual(status,2,result)
        self.assertIn('known input',result['error'])

    def test_compare_retains_validation_warnings_in_result_and_receipt(self):
        base=json.loads((ROOT/'examples/checkout-platform.base.architecture.json').read_text())
        head=json.loads((ROOT/'examples/checkout-platform.head.architecture.json').read_text())
        base['meta']['locale']='fr';head['meta']['locale']='fr'
        base_path=self.root/'base.json';head_path=self.root/'head.json';output=self.root/'delta.html'
        base_path.write_text(json.dumps(base));head_path.write_text(json.dumps(head))
        status,result=self.invoke('compare','architecture',base_path,head_path,output)
        self.assertEqual(status,0,result)
        self.assertIn('i18n/locale-fallback',[entry['code'] for entry in result['diagnostics']])
        receipt=json.loads(output.with_suffix('.python-delivery.json').read_text())
        self.assertIn('i18n/locale-fallback',[entry['code'] for entry in receipt['diagnostics']])

    def test_comparison_candidate_tamper_has_check_stage_and_integrity_diagnostic(self):
        base=self.root/'base.json';head=self.root/'head.json'
        base.write_bytes((ROOT/'examples/checkout-platform.base.architecture.json').read_bytes())
        head.write_bytes((ROOT/'examples/checkout-platform.head.architecture.json').read_bytes())
        self.assertEqual(self.invoke('compare','architecture',base,head,self.output)[0],0)
        base.write_bytes(base.read_bytes()+b'\n')
        status,result=self.invoke('check',self.output)
        self.assertEqual(status,1,result)
        self.assertEqual(result['stage'],'check')
        self.assertEqual(result['diagnostics'][0]['code'],'integrity/specification-mismatch')

    def test_required_comparison_browser_unavailable_retains_artifact_and_receipt(self):
        base=self.root/'base.json';head=self.root/'head.json'
        base.write_bytes((ROOT/'examples/checkout-platform.base.architecture.json').read_bytes())
        head.write_bytes((ROOT/'examples/checkout-platform.head.architecture.json').read_bytes())
        cases=[]

        original_import=__import__
        def missing_playwright(name,*args,**kwargs):
            if name.startswith('playwright'):
                raise ModuleNotFoundError("No module named 'playwright'")
            return original_import(name,*args,**kwargs)
        cases.append(('missing-module',lambda:patch('builtins.__import__',side_effect=missing_playwright),
                      'skipped','browser/unavailable'))

        class Chromium:
            executable_path='/definitely/missing/chromium'
        class Manager:
            chromium=Chromium()
            def __enter__(self):return self
            def __exit__(self,*args):pass
        playwright=types.ModuleType('playwright');playwright.__path__=[]
        sync_api=types.ModuleType('playwright.sync_api')
        sync_api.sync_playwright=lambda:Manager()
        playwright.sync_api=sync_api
        cases.append(('missing-executable',lambda:patch.dict(sys.modules,{
            'playwright':playwright,'playwright.sync_api':sync_api}),
                      'skipped','browser/unavailable'))

        class BrokenChromium:
            executable_path=__file__
            def launch(self,**kwargs):raise RuntimeError('fixture browser launch failure')
        class BrokenManager:
            chromium=BrokenChromium()
            def __enter__(self):return self
            def __exit__(self,*args):pass
        broken_playwright=types.ModuleType('playwright');broken_playwright.__path__=[]
        broken_sync_api=types.ModuleType('playwright.sync_api')
        broken_sync_api.sync_playwright=lambda:BrokenManager()
        broken_playwright.sync_api=broken_sync_api
        cases.append(('runtime-failure',lambda:patch.dict(sys.modules,{
            'playwright':broken_playwright,'playwright.sync_api':broken_sync_api}),
                      'failed','browser/runtime-error'))

        for name,dependency_patch,browser_status,diagnostic_code in cases:
            with self.subTest(name=name):
                output=self.root/(name+'.html')
                with dependency_patch():
                    status,result=self.invoke('compare','architecture',base,head,output,
                                              '--browser','required')
                self.assertEqual(status,1,result)
                self.assertEqual(result['stage'],'browser')
                self.assertTrue(output.is_file())
                self.assertEqual(result['staticChecks']['status'],'passed')
                self.assertEqual(result['browserEvidence']['status'],browser_status)
                self.assertEqual(result['diagnostics'][0]['code'],diagnostic_code)
                receipt=json.loads(output.with_suffix('.python-delivery.json').read_text())
                self.assertFalse(receipt['ok'])
                self.assertEqual(receipt['stage'],'browser')
                self.assertEqual(receipt['browserEvidence']['status'],browser_status)
                self.assertEqual(receipt['staticChecks']['status'],'passed')

    def test_output_only_comparison_check_fails_when_required_browser_is_unavailable(self):
        base=self.root/'base.json';head=self.root/'head.json';output=self.root/'delta.html'
        base.write_bytes((ROOT/'examples/checkout-platform.base.architecture.json').read_bytes())
        head.write_bytes((ROOT/'examples/checkout-platform.head.architecture.json').read_bytes())
        self.assertEqual(self.invoke('compare','architecture',base,head,output)[0],0)

        original_import=__import__
        def missing_playwright(name,*args,**kwargs):
            if name.startswith('playwright'):
                raise ModuleNotFoundError("No module named 'playwright'")
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=missing_playwright):
            status,result=self.invoke('check',output,'--browser','required')
        self.assertEqual(status,1,result)
        self.assertEqual(result['stage'],'browser')
        self.assertEqual(result['browserEvidence']['status'],'skipped')
        self.assertEqual(result['diagnostics'][0]['code'],'browser/unavailable')

    @unittest.skipUnless(sys.platform=='darwin','macOS directory-alias regression')
    def test_tmp_alias_comparison_check_resolves_recorded_output(self):
        with tempfile.TemporaryDirectory(prefix='archify-alias-',dir='/tmp')as tmp:
            output=Path(tmp)/'delta.html'
            self.assertEqual(self.invoke('compare','architecture',ROOT/'examples/checkout-platform.base.architecture.json',ROOT/'examples/checkout-platform.head.architecture.json',output)[0],0)
            self.assertEqual(self.invoke('check',output,'--require-provenance')[0],0)

    def test_inspection_rejects_receipt_and_candidate_changes(self):
        from unittest.mock import patch
        for typed, mutate_receipt in [(True, True), (False, True), (False, False)]:
            self.assertEqual(self.invoke('finalize','architecture',self.source,self.output)[0],0)
            before=self.output.read_bytes()
            def inspect(*args):
                if mutate_receipt:
                    receipt=self.output.with_suffix('.python-delivery.json')
                    saved=json.loads(receipt.read_text());saved['visualReview']='changed';receipt.write_text(json.dumps(saved))
                else:self.source.write_bytes(self.source.read_bytes()+b'\n')
                return {'status':'skipped','reason':'test mutation'}
            with patch('archify_runtime.browser.check_browser',side_effect=inspect):
                command=('check','architecture',self.source,self.output) if typed else ('check',self.output)
                status,result=self.invoke(*command)
            self.assertNotEqual(status,0,result)
            self.assertEqual(self.output.read_bytes(),before)
            self.output.with_suffix('.python-delivery.json').unlink()
            self.output.with_suffix('.delivery.json').unlink()

    def test_comparison_inspection_rejects_changed_candidate(self):
        from unittest.mock import patch
        base=self.root/'base.json';head=self.root/'head.json'
        base.write_bytes((ROOT/'examples/checkout-platform.base.architecture.json').read_bytes())
        head.write_bytes((ROOT/'examples/checkout-platform.head.architecture.json').read_bytes())
        self.assertEqual(self.invoke('compare','architecture',base,head,self.output)[0],0)
        def inspect(*args):
            base.write_bytes(base.read_bytes()+b'\n')
            return {'status':'skipped','reason':'test mutation'}
        with patch('archify_runtime.commands.check_comparison_browser',side_effect=inspect):
            status,result=self.invoke('check',self.output)
        self.assertNotEqual(status,0,result)
