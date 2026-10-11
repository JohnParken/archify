"""Fault injection, recovery ownership and portable repeated publication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from archify_runtime.common import DiagnosticError
from archify_runtime.delivery import publish_transaction, recover, sidecar
from archify_runtime.integrity import CONTRACT, receipt_path
from archify_runtime.output import snapshot

class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="archify-txn-", dir=str(Path(tempfile.gettempdir()).resolve()))
        self.directory = Path(self.temp.name)
        self.target = self.directory / 'diagram.html'
        self.source = self.directory / 'candidate.json'
        self.source.write_bytes(b'{}')
    def tearDown(self):
        self.temp.cleanup()
    def receipt(self, artifact):
        identity = lambda b: {'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)}
        return {'schemaVersion':1,'deliveryContract':CONTRACT,'input':str(self.source),
                'output':str(self.target),'type':'sequence','specification':identity(b'{}'),
                'artifact':identity(artifact),'ok':True}
    def publish(self, content=b'new', identifier='01234567-abcd'):
        return publish_transaction(self.target,content,snapshot(self.target),identifier,self.receipt(content),
                lambda:{'status':'passed'})
    def test_repeated_write_without_unix_fchmod_preserves_permissions(self):
        self.publish(b'first')
        if os.name != 'nt':self.target.chmod(0o640)
        with patch.object(os,'fchmod',None,create=True):self.publish(b'second')
        self.assertEqual(self.target.read_bytes(),b'second')
        if os.name != 'nt':self.assertEqual(self.target.stat().st_mode&0o777,0o640)
        self.assertEqual(json.loads(sidecar(self.target,'.delivery.json').read_text())['artifact'],self.receipt(b'second')['artifact'])
    def test_failure_between_artifact_and_receipt_restores_previous_pair(self):
        self.publish(b'first')
        old=[p.read_bytes() for p in [self.target,receipt_path(self.target),sidecar(self.target,'.delivery.json')]]
        real=os.replace
        def fail_receipt(source,target):
            if Path(source).name=='new-1':raise OSError('injected receipt replacement failure')
            return real(source,target)
        with patch('archify_runtime.delivery.os.replace',side_effect=fail_receipt):
            with self.assertRaises(OSError):self.publish(b'second')
        self.assertEqual(old,[p.read_bytes()for p in [self.target,receipt_path(self.target),sidecar(self.target,'.delivery.json')]])
        self.assertFalse(sidecar(self.target,'.delivery-pending.json').exists())
        self.assertFalse((self.directory/'.archify-delivery-lock.json').exists())
    def crash_child(self):
        # A separate process dies after HTML replacement, leaving durable journal/lock.
        code='''import os,sys
from pathlib import Path
from archify_runtime.delivery import publish_transaction
from archify_runtime.output import snapshot
import json
real=os.replace
out=Path(sys.argv[1]); receipt=json.loads(sys.argv[2])
def crash(source,target):
 real(source,target)
 if Path(source).name=='new-0':os._exit(73)
os.replace=crash
publish_transaction(out,b'new',snapshot(out),'01234567-abcd',receipt,lambda:{'status':'passed'})
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.target),json.dumps(self.receipt(b'new'))],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode,73)
    def test_process_crash_recovers_previous_bytes_and_receipts(self):
        self.publish(b'first')
        self.crash_child()
        self.assertEqual(self.target.read_bytes(),b'new')
        self.assertTrue(sidecar(self.target,'.delivery-pending.json').exists())
        self.assertTrue(recover(self.target)['ok'])
        self.assertEqual(self.target.read_bytes(),b'first')
        self.assertEqual(json.loads(receipt_path(self.target).read_text())['artifact'],self.receipt(b'first')['artifact'])
        self.assertFalse((self.directory/'.archify-delivery-lock.json').exists())
    def test_crash_first_publication_removes_owned_new_artifact(self):
        self.crash_child()
        recover(self.target)
        self.assertFalse(self.target.exists())
    def test_recovery_preserves_foreign_claimant(self):
        self.publish(b'first');self.crash_child();self.target.write_bytes(b'foreign')
        with self.assertRaises(DiagnosticError):recover(self.target)
        self.assertEqual(self.target.read_bytes(),b'foreign')
        self.assertTrue(sidecar(self.target,'.delivery-pending.json').exists())
    def test_unknown_lock_and_receipt_preserved(self):
        lock=self.directory/'.archify-delivery-lock.json';lock.write_text('foreign')
        with self.assertRaises(DiagnosticError):self.publish()
        self.assertEqual(lock.read_text(),'foreign');lock.unlink()
        receipt_path(self.target).write_text('foreign')
        with self.assertRaises(DiagnosticError):self.publish()
        self.assertEqual(receipt_path(self.target).read_text(),'foreign')
        self.assertFalse(self.target.exists())

    def test_preexisting_staging_directory_preserved(self):
        folder=self.directory/'.archify-txn-01234567-abcd'
        folder.mkdir();(folder/'foreign').write_bytes(b'keep')
        with self.assertRaises(FileExistsError):self.publish()
        self.assertEqual((folder/'foreign').read_bytes(),b'keep')
        self.assertFalse(self.target.exists())
    def test_crash_after_commit_decision_finishes_without_rollback(self):
        self.publish(b'first')
        code="""import os,sys,json
from pathlib import Path
import archify_runtime.delivery as delivery
from archify_runtime.output import snapshot
out=Path(sys.argv[1]);receipt=json.loads(sys.argv[2])
delivery._complete=lambda *args,**kwargs:os._exit(74)
delivery.publish_transaction(out,b'new',snapshot(out),'01234567-abcd',receipt,lambda:{'status':'passed'})
"""
        result=subprocess.run([sys.executable,'-c',code,str(self.target),json.dumps(self.receipt(b'new'))],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode,74)
        self.assertEqual(recover(self.target)['status'],'committed')
        self.assertEqual(self.target.read_bytes(),b'new')
        self.assertFalse(sidecar(self.target,'.delivery-pending.json').exists())
        self.publish(b'after-recovery')
    def test_recovery_after_partial_lock_cleanup(self):
        self.crash_child()
        # Rollback is durably marked before cleanup; simulate crash before journal retirement.
        real=os.unlink
        def crash(path,*args,**kwargs):
            if Path(path)==sidecar(self.target,'.delivery-pending.json'):raise OSError('journal retirement interrupted')
            return real(path,*args,**kwargs)
        with patch('archify_runtime.delivery.os.unlink',side_effect=crash):
            with self.assertRaises(OSError):recover(self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(recover(self.target)['status'],'rolled-back')

    def test_journal_and_lock_receipt_aliases_rejected_before_writes(self):
        for extra in [sidecar(self.target,'.delivery-pending.json'),self.directory/'.archify-delivery-lock.json']:
            receipt=self.receipt(b'new');receipt['requestedReceipt']=str(extra)
            with self.assertRaises(DiagnosticError):publish_transaction(self.target,b'new',None,'01234567-abcd',receipt,None)
            self.assertFalse(self.target.exists());self.assertFalse(extra.exists())
    def test_recovery_retries_owned_phase_candidate_after_interruption(self):
        self.publish(b'first');self.crash_child()
        real=os.replace
        def interrupt(source,target):
            if Path(source).name=='journal-rolled-back':raise OSError('interrupted phase replacement')
            return real(source,target)
        with patch('archify_runtime.delivery.os.replace',side_effect=interrupt):
            with self.assertRaises(OSError):recover(self.target)
        self.assertEqual(recover(self.target)['status'],'rolled-back')
        self.assertEqual(self.target.read_bytes(),b'first')

    def test_live_owner_blocks_recovery_and_concurrent_process_publication(self):
        import time
        ready=self.directory/'ready';release=self.directory/'release'
        code="""import os,sys,json,time
from pathlib import Path
from archify_runtime.delivery import publish_transaction
from archify_runtime.output import snapshot
out=Path(sys.argv[1]);receipt=json.loads(sys.argv[2]);ready=Path(sys.argv[3]);release=Path(sys.argv[4])
real=os.replace
def pause(source,target):
 real(source,target)
 if Path(source).name=='new-0':
  ready.write_text('ready')
  end=time.monotonic()+10
  while not release.exists() and time.monotonic()<end:time.sleep(.01)
os.replace=pause
publish_transaction(out,b'new',snapshot(out),'01234567-abcd',receipt,lambda:{'status':'passed'})
"""
        child=subprocess.Popen([sys.executable,'-c',code,str(self.target),json.dumps(self.receipt(b'new')),str(ready),str(release)],env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1])})
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(ready.exists())
            with self.assertRaises(DiagnosticError):recover(self.target)
            with self.assertRaises(DiagnosticError):self.publish(b'competing')
            self.assertEqual(self.target.read_bytes(),b'new')
        finally:
            release.write_text('release');child.wait(timeout=5)
        self.assertEqual(child.returncode,0)
        self.assertFalse(sidecar(self.target,'.delivery-pending.json').exists())
