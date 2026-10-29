"""Offline mocked controls only, never Windows timing/identity proof."""
import importlib.util,json,os
from pathlib import Path
import subprocess,tempfile,unittest
from unittest import mock
HERE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phase_probe_contract',HERE/'probe.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

def markers(stages=None,pid=123):
 return ''.join('VSE_OS_STAGE|'+stage+'|'+str(pid)+'|'+str(i+1)+'|1000|2026-10-29T05:00:00Z\n' for i,stage in enumerate(stages or p.STAGES)).encode()

class PhaseContracts(unittest.TestCase):
 def source(self):return Path(os.environ.get('VSE_REVIEW_SOURCE','identity-source'))/'scripts/windows_verification.py'
 def test_exact_C45_source_keeps_literal_original_command_and60budget(self):
  command=p.original_command(self.source());self.assertIn('Get-CimInstance',command)
  self.assertEqual(p.TIMEOUT,60)
  marked=p.marked_command(command)
  for stage in p.STAGES:self.assertIn("VseOsStage '"+stage+"'",marked)
  self.assertEqual(marked.count('Get-CimInstance Win32_OperatingSystem'),1)
  self.assertEqual(marked.count("Get-ItemProperty 'HKLM:"),1)
 def test_foreign_source_and_changed_anchor_rejected(self):
  with tempfile.TemporaryDirectory() as temp:
   path=Path(temp)/'source.py';path.write_bytes(self.source().read_bytes()+b'changed')
   with self.assertRaises(ValueError):p.original_command(path)
  with self.assertRaises(ValueError):p.marked_command('$os = different')
 def test_stage_order_pid_and_timestamp_fail_closed(self):
  self.assertEqual([v['stage'] for v in p.stages_from(markers(),123)],list(p.STAGES))
  for bad in (markers(pid=999),markers(['after_cim']),markers().replace(b'|1000|',b'|0|'),markers().replace(b'2026-10-29T05:00:00Z',b'not UTC')):
   with self.subTest(bad=bad[:50]),self.assertRaises(ValueError):p.stages_from(bad,123)
 def capture(self,root,timed_out=False,invalid=False):
  class Child:
   pid=123;returncode=0
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def communicate(self,timeout=None):
    if timed_out and timeout is not None:raise subprocess.TimeoutExpired(['MOCK'],60,output=b'partial',stderr=b'partial')
    return b'{}',markers(p.STAGES[:2] if timed_out else None)
   def kill(self):self.returncode=-9
  validate=mock.Mock(side_effect=ValueError('MOCK mismatch') if invalid else None)
  with mock.patch.object(p.subprocess,'Popen',return_value=Child()) as start:
   report=p.run_capture(Path('MOCK-NOT-EXECUTED'),p.marked_command(p.original_command(self.source())),root,{'SAFE':'MOCK'},root/'capture',True,validate)
  self.assertEqual(start.call_count,1);return report,validate
 def test_success_requires_all_stages_and_measured_identity(self):
  with tempfile.TemporaryDirectory() as temp:
   r,v=self.capture(Path(temp));self.assertEqual(r['result'],'passed');v.assert_called_once_with({})
 def test_timeout_preserves_complete_partial_stream_without_duplicate_prefix(self):
  with tempfile.TemporaryDirectory() as temp:
   r,v=self.capture(Path(temp),True);self.assertEqual(r['result'],'failed');self.assertTrue(r['timed_out'])
   self.assertEqual([s['stage'] for s in r['stages']],list(p.STAGES[:2]));v.assert_not_called()
   self.assertEqual((Path(temp)/'capture/stdout.bin').read_bytes(),b'{}')
   self.assertIn('60-second',r['error'])
 def test_invalid_identity_is_failure_even_with_all_stages(self):
  with tempfile.TemporaryDirectory() as temp:
   r,_=self.capture(Path(temp),invalid=True);self.assertEqual(r['result'],'failed');self.assertIn('mismatch',r['error'])
 def test_nonwindows_cannot_emit_native_measurements(self):
  with mock.patch.object(p.os,'name','posix'),self.assertRaisesRegex(RuntimeError,'Native Windows'):
   p.main(['--source','unused','--output','unused','--runner','windows-2022'])

if __name__=='__main__':unittest.main()
