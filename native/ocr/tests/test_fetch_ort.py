import hashlib,json,pathlib,tempfile,unittest,zipfile
from fetch_ort import fetch,safe_relative
class FetchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.r=pathlib.Path(self.tmp.name);self.cache=self.r/'cache';self.cache.mkdir();self.archive=self.cache/'sdk.zip'
  with zipfile.ZipFile(self.archive,'w') as z:z.writestr('sdk/lib/runtime.dll',b'library');z.writestr('sdk/LICENSE',b'license');z.writestr('sdk/huge.pdb',b'not extracted');z.writestr('sdk/include/api.h',b'header')
  fs=[]
  for p,role,b in [('lib/runtime.dll','runtime',b'library'),('LICENSE','notice',b'license'),('include/api.h','header',b'header')]:fs.append(dict(path=p,role=role,bytes=len(b),sha256=hashlib.sha256(b).hexdigest()))
  self.rec=dict(archive=dict(filename='sdk.zip',bytes=self.archive.stat().st_size,sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest(),url='https://unused.invalid/sdk.zip'),archive_root='sdk',git_commit='test',files=fs,symlinks={})
  self.m=self.r/'manifest.json';self.save()
 def save(self):self.m.write_text(json.dumps(dict(version='test',targets={'windows-x64':self.rec})))
 def tearDown(self):self.tmp.cleanup()
 def runfetch(self,**kw):return fetch('windows-x64',self.r/'out',self.cache,self.m,**kw)
 def test_allowlisted_and_idempotent(self):
  out=self.runfetch();self.assertFalse((out/'huge.pdb').exists());self.assertEqual(self.runfetch(),out)
 def test_runtime_only(self):
  out=self.runfetch(runtime_only=True);self.assertFalse((out/'include/api.h').exists());self.assertTrue((out/'LICENSE').exists())
 def test_cache_tamper(self):
  self.archive.write_bytes(b'bad')
  with self.assertRaises(ValueError):self.runfetch()
 def test_existing_output_tamper(self):
  out=self.runfetch();(out/'lib/runtime.dll').write_bytes(b'corrupt')
  with self.assertRaises(ValueError):self.runfetch()
 def test_paths(self):
  for value in ['../outside','/absolute','C:/absolute','a\\b']:
   with self.assertRaises(ValueError):safe_relative(value)
 def test_extracted_hash_mismatch(self):
  self.rec['files'][0]['sha256']='0'*64;self.save()
  with self.assertRaises(ValueError):self.runfetch()
  self.assertFalse((self.r/'out').exists())
 def test_offline_cache_hit(self):self.assertTrue(self.runfetch(offline=True).is_dir())
 def test_offline_cache_miss_no_network(self):
  from unittest.mock import patch
  self.archive.unlink()
  with patch('urllib.request.urlopen',side_effect=AssertionError('network must not be called')):
   with self.assertRaises(FileNotFoundError):self.runfetch(offline=True)
 def test_malicious_manifest_path(self):
  self.rec['files'][0]['path']='../escape';self.save()
  with self.assertRaises(ValueError):self.runfetch()
  self.assertFalse((self.r/'escape').exists())
 def test_existing_bad_symlink(self):
  self.rec['symlinks']={'lib/alias.dll':'runtime.dll'};self.save()
  out=self.runfetch();(out/'lib/alias.dll').unlink();(out/'lib/alias.dll').symlink_to('wrong.dll')
  with self.assertRaises(ValueError):self.runfetch()
if __name__=='__main__':unittest.main()
