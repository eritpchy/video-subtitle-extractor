import hashlib,io,json,tarfile,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import prepare_models as m
class Assets(unittest.TestCase):
 def test_pins_complete(self):
  p=m.load_pins(m.HERE/'model-source-pins.json');self.assertEqual(len(p['models']),15);self.assertEqual(p['converter']['optimizer'],'None');self.assertFalse(p['converter']['autoinstall'])
 def test_bad_source_rejected(self):
  p=m.load_pins(m.HERE/'model-source-pins.json');p['models'][0]['url']='https://example.invalid/model.tar'
  with tempfile.TemporaryDirectory() as t:
   f=Path(t)/'pins.json';f.write_text(json.dumps(p))
   with self.assertRaises(ValueError):m.load_pins(f)
 def test_duplicate_rejected(self):
  p=m.load_pins(m.HERE/'model-source-pins.json');p['models'][1]=p['models'][0]
  with tempfile.TemporaryDirectory() as t:
   f=Path(t)/'pins.json';f.write_text(json.dumps(p))
   with self.assertRaises(ValueError):m.load_pins(f)
 def test_versions_fail_closed(self):
  with patch('importlib.metadata.version',return_value='0'):
   with self.assertRaises(RuntimeError):m.check_versions({'tool_versions':{'paddle2onnx':'2.1.0'}})
 def test_corrupt_cache_not_replaced(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);row={'archive_sha256':'a'*64};f=root/(row['archive_sha256']+'.tar');f.write_bytes(b'bad')
   with self.assertRaises(ValueError):m.get_archive(row,root,True)
   self.assertEqual(f.read_bytes(),b'bad')
 def test_offline_no_download(self):
  with tempfile.TemporaryDirectory() as t,patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
   with self.assertRaises(FileNotFoundError):m.get_archive({'archive_sha256':'a'*64},Path(t),True)
 def test_extract_hash_and_traversal(self):
  for path,valid in [('model/inference.json',True),('../inference.json',False)]:
   with tempfile.TemporaryDirectory() as t:
    root=Path(t);archive=root/'a.tar';data=b'{}'
    with tarfile.open(archive,'w') as tf:
     info=tarfile.TarInfo(path);info.size=len(data);tf.addfile(info,io.BytesIO(data))
    row={'files':{'inference.json':hashlib.sha256(data).hexdigest()}}
    if valid:self.assertEqual((m.extract_sources(archive,row,root/'out')/'inference.json').read_bytes(),data)
    else:
     with self.assertRaises(ValueError):m.extract_sources(archive,row,root/'out')
 def test_wrong_source_hash(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'a.tar'
   with tarfile.open(archive,'w') as tf:
    info=tarfile.TarInfo('inference.json');info.size=2;tf.addfile(info,io.BytesIO(b'{}'))
   with self.assertRaises(ValueError):m.extract_sources(archive,{'files':{'inference.json':'a'*64}},root/'out')
 def test_converter_contract_rejected(self):
  p=m.load_pins(m.HERE/'model-source-pins.json');p['converter']['optimizer']='polygraphy'
  with tempfile.TemporaryDirectory() as t:
   f=Path(t)/'pins.json';f.write_text(json.dumps(p))
   with self.assertRaises(ValueError):m.load_pins(f)
 def test_archive_symlink_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);archive=root/'a.tar'
   with tarfile.open(archive,'w') as tf:
    info=tarfile.TarInfo('inference.json');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';tf.addfile(info)
   with self.assertRaises(ValueError):m.extract_sources(archive,{'files':{'inference.json':'a'*64}},root/'out')
 def test_canonical_json(self):
  self.assertEqual(m.canonical({'z':'字幕','a':1}),m.canonical({'a':1,'z':'字幕'}));self.assertTrue(m.canonical({}).endswith(b'\n'))
class PreflightTests(unittest.TestCase):
 def pins(self):return m.load_pins(m.HERE/'model-source-pins.json')
 def version(self,name):return m.declared_build_versions()[name.lower().replace('_','-').replace('.','-')]
 def test_explicit_runtime_closure(self):
  versions=m.declared_build_versions()
  for name in ['packaging','polygraphy','httpx','httpcore','anyio','certifi','idna','h11','pillow','opt-einsum','networkx','typing-extensions','safetensors','setuptools']:
   self.assertIn(name,versions)
  self.assertEqual(versions['packaging'],'26.3')
 def test_preflight_runs_real_cli_import_in_isolated_interpreter(self):
  from subprocess import CompletedProcess
  with patch.object(m.importlib.metadata,'version',side_effect=self.version),patch.object(m.subprocess,'run',return_value=CompletedProcess([],0,stdout='ok')) as run:
   self.assertEqual(m.preflight_tools(self.pins())['packaging'],'26.3')
  args,kw=run.call_args
  self.assertEqual(args[0][:3],[m.sys.executable,'-I','-c'])
  self.assertIn('paddle2onnx.command',args[0][3]);self.assertIn('paddle2onnx.paddle2onnx_cpp2py_export',args[0][3])
  self.assertEqual(kw['env']['PIP_NO_INDEX'],'1');self.assertEqual(kw['env']['POLYGRAPHY_AUTOINSTALL_DEPS'],'0')
 def test_missing_packaging_fails_before_import_probe(self):
  def version(name):
   if name=='packaging':raise m.importlib.metadata.PackageNotFoundError(name)
   return self.version(name)
  with patch.object(m.importlib.metadata,'version',side_effect=version),patch.object(m.subprocess,'run') as run:
   with self.assertRaisesRegex(RuntimeError,'Missing conversion dependency packaging==26.3'):m.preflight_tools(self.pins())
   run.assert_not_called()
 def test_metadata_present_but_broken_import_is_caught(self):
  from subprocess import CompletedProcess
  with patch.object(m.importlib.metadata,'version',side_effect=self.version),patch.object(m.subprocess,'run',return_value=CompletedProcess([],1,stdout='ModuleNotFoundError: packaging')):
   with self.assertRaisesRegex(RuntimeError,'preflight failed before downloading models'):m.preflight_tools(self.pins())
 def test_preflight_timeout_is_actionable(self):
  with patch.object(m.importlib.metadata,'version',side_effect=self.version),patch.object(m.subprocess,'run',side_effect=m.subprocess.TimeoutExpired('test',120)):
   with self.assertRaisesRegex(RuntimeError,'preflight timed out'):m.preflight_tools(self.pins())
 def test_preflight_failure_precedes_archive_access(self):
  from types import SimpleNamespace
  with patch.object(m,'check_versions'),patch.object(m,'preflight_tools',side_effect=RuntimeError('dependency failure')),patch.object(m,'get_archive') as archive:
   with self.assertRaisesRegex(RuntimeError,'dependency failure'):m.prepare(SimpleNamespace(pins=m.HERE/'model-source-pins.json'))
   archive.assert_not_called()
 def test_dependency_environment_cannot_enable_autoinstall(self):
  env=m.conversion_environment(PIP_NO_INDEX='0',POLYGRAPHY_AUTOINSTALL_DEPS='1')
  self.assertEqual(env['PIP_NO_INDEX'],'1');self.assertEqual(env['POLYGRAPHY_AUTOINSTALL_DEPS'],'0')
 def test_loose_requirements_rejected(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'r.txt';p.write_text('packaging>=24\n')
   with self.assertRaises(ValueError):m.declared_build_versions(p)
if __name__=='__main__':unittest.main()
