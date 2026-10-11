"""Stable identity comparison, independent mutations and publication checks."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from archify_runtime.comparison import compare_architecture,canonical_architecture,canonical_json,render_comparison
from archify_runtime.commands import check_comparison_html
from archify_runtime.architecture import render_architecture
from archify_runtime.common import DiagnosticError
ROOT=Path(__file__).resolve().parents[2]
class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.base=json.loads((ROOT/'examples/checkout-platform.base.architecture.json').read_text())
        self.head=json.loads((ROOT/'examples/checkout-platform.head.architecture.json').read_text())
    def test_semantic_evidence_geometry_and_topology_classification(self):
        changed=copy.deepcopy(self.base)
        changed['components'][0]['label']='New service'
        changed['components'][1]['sources']=[{'path':'src/a.py','line':1}]
        changed['components'][2]['pos'][0]+=10
        changed['connections'][0]['from']=changed['components'][2]['id']
        result=compare_architecture(self.base,changed)
        self.assertEqual(result['summary']['components'],{'added':0,'changed':1,'evidenceChanged':1,'removed':0,'moved':1})
        self.assertEqual(result['summary']['connections']['changed'],1)
        self.assertEqual(result['proofLevel'],'authored')
    def test_reordering_and_output_location_are_not_semantic_changes(self):
        changed=copy.deepcopy(self.base)
        for key in ['components','connections','boundaries']:changed[key].reverse()
        for b in changed['boundaries']:b['wraps'].reverse()
        changed['meta']['output']='different.html'
        self.assertEqual(canonical_json(canonical_architecture(self.base)),canonical_json(canonical_architecture(changed)))
        self.assertFalse(any(compare_architecture(self.base,changed)['changes'].values()))
    def test_missing_duplicate_disjoint_and_ambiguous_identity_rejected(self):
        for kind in ['missing','duplicate','disjoint','boundary']:
            changed=copy.deepcopy(self.base)
            if kind=='missing':del changed['connections'][0]['id']
            elif kind=='duplicate':changed['connections'].append(copy.deepcopy(changed['connections'][0]))
            elif kind=='disjoint':
                for c in changed['components']:c['id']='new-'+c['id']
            else:changed['boundaries'].append(copy.deepcopy(changed['boundaries'][0]))
            with self.subTest(kind=kind),self.assertRaises(DiagnosticError):compare_architecture(self.base,changed)
    def test_html_has_real_svg_namespaces_and_unique_resolved_ids(self):
        result=compare_architecture(self.base,self.head)
        template=(ROOT/'assets/template.html').read_text()
        html=render_comparison(result,render_architecture(self.base),render_architecture(self.head),template)
        check_comparison_html(html)
        self.assertNotIn(b'<ns0:svg',html)
        self.assertIn(b'data-tab="delta"',html)
    def test_cli_receipts_repeat_check_and_tampering(self):
        with tempfile.TemporaryDirectory(prefix='archify-delta-')as tmp:
            output=Path(tmp).resolve()/'delta.html'
            args=[sys.executable,str(ROOT/'bin/archify.py'),'compare','architecture',str(ROOT/'examples/checkout-platform.base.architecture.json'),str(ROOT/'examples/checkout-platform.head.architecture.json'),str(output),'--json']
            for _ in range(2):
                run=subprocess.run(args,capture_output=True,text=True)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            check=[sys.executable,str(ROOT/'bin/archify.py'),'check',str(output),'--require-provenance','--json']
            self.assertEqual(subprocess.run(check,capture_output=True).returncode,0)
            output.write_bytes(output.read_bytes()+b'changed')
            self.assertNotEqual(subprocess.run(check,capture_output=True).returncode,0)
            self.assertTrue(output.with_suffix('.receipt.json').exists())

    def test_required_browser_failure_preserves_static_artifact_and_failed_receipt(self):
        from unittest.mock import patch
        from contextlib import redirect_stdout
        from io import StringIO
        from archify_runtime.commands import advanced_main
        with tempfile.TemporaryDirectory(prefix='archify-delta-gate-')as tmp:
            output=Path(tmp).resolve()/'delta.html'
            with patch('archify_runtime.commands.check_comparison_browser',side_effect=RuntimeError('browser failed')),redirect_stdout(StringIO()):
                status=advanced_main(['compare','architecture',str(ROOT/'examples/checkout-platform.base.architecture.json'),str(ROOT/'examples/checkout-platform.head.architecture.json'),str(output),'--browser','required','--json'])
            self.assertEqual(status,1)
            self.assertTrue(output.is_file())
            check_comparison_html(output.read_bytes())
            receipt=json.loads(output.with_suffix('.python-delivery.json').read_text())
            self.assertFalse(receipt['ok'])
            self.assertEqual(receipt['stage'],'browser')
            self.assertEqual(receipt['staticChecks']['status'],'passed')
            self.assertEqual(receipt['browserEvidence']['status'],'failed')
    def test_small_source_node_rejected_instead_of_badge_overlap(self):
        doc=copy.deepcopy(self.base);doc['components']=doc['components'][:1];doc['connections']=[];doc['boundaries']=[];doc['components'][0]['size']=[140,30];doc['components'][0]['label']='Long Source Label'
        doc['components'][0]['sources']=[{'path':'source.py','line':1}]
        with self.assertRaises(DiagnosticError)as error:render_architecture(doc)
        self.assertIn('source-evidence/node-footprint',[d['code']for d in error.exception.diagnostics])
    def test_canonical_unicode_order_and_javascript_exponents(self):
        value={'\ue000':1,'\U00010000':2,'number':1e-8}
        self.assertEqual(canonical_json(value),'{'+'"number":1e-8,"\U00010000":2,"\ue000":1}')
        self.assertEqual(canonical_json(1e20),'100000000000000000000')
        self.assertEqual(canonical_json(1e21),'1e+21')
