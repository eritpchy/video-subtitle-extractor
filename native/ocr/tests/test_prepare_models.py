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
if __name__=='__main__':unittest.main()
