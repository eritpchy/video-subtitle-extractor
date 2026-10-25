import hashlib,json,pathlib,struct,tempfile,unittest
from unittest.mock import patch
import vc_runtime_helper as h
class VCTests(unittest.TestCase):
 def test_synthetic_four_dll_hashes_and_machine(self):
  # Unit fixture only: no Microsoft binaries or license acceptance in the repo.
  with tempfile.TemporaryDirectory() as t:
   root=pathlib.Path(t);rows=[]
   for name in ['license.rtf','msvcp140.dll','msvcp140_1.dll','vcruntime140.dll','vcruntime140_1.dll']:
    body=bytearray(128);body[:2]=b'MZ';struct.pack_into('<I',body,0x3c,64);body[64:68]=b'PE\0\0';struct.pack_into('<H',body,68,0x8664)
    data=b'synthetic test license' if name=='license.rtf' else bytes(body)+name.encode()
    (root/name).write_bytes(data);rows.append({'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
   with patch.object(h,'load_manifest',return_value={'files':rows}):
    m,values=h.verify_source(root,root/'license.rtf');self.assertEqual(len(values),5)
 def test_license_assertion_required(self):
  with self.assertRaises(PermissionError):h.fetch('unused','unused')
 def test_offline_never_downloads(self):
  with tempfile.TemporaryDirectory() as t,patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')):
   with self.assertRaises(FileNotFoundError):h.fetch(pathlib.Path(t)/'out',pathlib.Path(t)/'cache',offline=True,acknowledge_license=True)
 def test_bad_archive_and_cab_fail(self):
  with self.assertRaises(ValueError):h.checked(b'bad',dict(bytes=3,sha256='0'*64))
  with self.assertRaises(ValueError):h.cab_files(b'bad',[])
 def test_malicious_manifest_path(self):
  with tempfile.TemporaryDirectory() as t:
   m=h.load_manifest();m['files'][0]['name']='../escape';p=pathlib.Path(t)/'m.json';p.write_text(json.dumps(m))
   with self.assertRaises(ValueError):h.load_manifest(p)
 def test_wrong_runtime_source(self):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t);(p/'msvcp140.dll').write_bytes(b'bad');(p/'license.rtf').write_bytes(b'bad')
   with self.assertRaises(ValueError):h.verify_source(p,p/'license.rtf')
if __name__=='__main__':unittest.main()
