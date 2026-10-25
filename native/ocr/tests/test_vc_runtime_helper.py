import hashlib,json,pathlib,struct,tempfile,unittest
from unittest.mock import patch
import vc_runtime_helper as h
class VCTests(unittest.TestCase):
 def test_synthetic_four_dll_hashes_and_machine(self):
  # Unit fixture only: no Microsoft binaries or license acceptance in the repo.
  with tempfile.TemporaryDirectory() as t:
   root=pathlib.Path(t);rows=[]
   for name in ['license.docx','license-2015-2022.docx','msvcp140.dll','msvcp140_1.dll','vcruntime140.dll','vcruntime140_1.dll']:
    body=bytearray(128);body[:2]=b'MZ';struct.pack_into('<I',body,0x3c,64);body[64:68]=b'PE\0\0';struct.pack_into('<H',body,68,0x8664)
    data=b'synthetic test license' if name.endswith('.docx') else bytes(body)+name.encode()
    (root/name).write_bytes(data);rows.append({'name':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
   with patch.object(h,'load_manifest',return_value={'files':rows,'license_file':'license.docx'}):
    m,values=h.verify_source(root,root/'license.docx');self.assertEqual(len(values),6)
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

# The current source changed its packaging, not the entitlement/runtime gates.
import io
import pytest


def test_committed_manifest_exact_runtime_and_independent_notices():
 m=h.load_manifest()
 assert m['version']=='14.51.36247.0' and m['architecture']=='x64'
 assert m['license_file']=='license.docx'
 assert m['installer']['sha256']=='843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c'
 assert m['runtime_cab']['member']=='a4'
 assert {r['name'] for r in m['files']}==h.DLL_NAMES|h.LICENSE_NAMES
 assert all(r['file_version']==m['version'] for r in m['files'] if r['name'] in h.DLL_NAMES)
 assert all(r['url'].startswith('https://visualstudio.microsoft.com/') for r in m['files'] if r['name'] in h.LICENSE_NAMES)


def source(data,url='https://visualstudio.microsoft.com/pinned-source'):
 return {'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'url':url}


def test_content_addressed_cache_ignores_legacy_pin_and_revalidates_hits(tmp_path):
 (tmp_path/'VC_redist.x64.exe').write_bytes(b'old pinned runtime')
 rec=source(b'new pinned runtime')
 with patch.object(h.urllib.request,'urlopen',return_value=io.BytesIO(b'new pinned runtime')) as download:
  assert h.cached_source(rec,tmp_path,'VC_redist.x64.exe')==b'new pinned runtime'
  download.assert_called_once_with(rec['url'],timeout=120)
 with patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')):
  assert h.cached_source(rec,tmp_path,'VC_redist.x64.exe',offline=True)==b'new pinned runtime'
  (tmp_path/rec['sha256']/'VC_redist.x64.exe').write_bytes(b'tampered')
  with pytest.raises(ValueError,match='integrity mismatch'):
   h.cached_source(rec,tmp_path,'VC_redist.x64.exe',offline=True)
 assert (tmp_path/'VC_redist.x64.exe').read_bytes()==b'old pinned runtime'


@pytest.mark.parametrize('name',['license.docx','license-2015-2022.docx'])
def test_independent_license_bad_hash_never_written(tmp_path,name):
 rec=source(b'reviewed exact notice')
 with patch.object(h.urllib.request,'urlopen',return_value=io.BytesIO(b'changed terms')):
  with pytest.raises(ValueError,match='integrity mismatch'):h.cached_source(rec,tmp_path,name)
 assert not (tmp_path/rec['sha256']/name).exists()


def test_cached_source_rejects_unsafe_digest_path_and_symlink(tmp_path):
 rec=source(b'x')
 with pytest.raises(ValueError):h.cached_source({**rec,'sha256':'../escape'},tmp_path,'license.docx')
 with pytest.raises(ValueError):h.cached_source(rec,tmp_path,'../escape')
 target=tmp_path/'target';target.mkdir();cache=tmp_path/'linked';cache.symlink_to(target,target_is_directory=True)
 with pytest.raises(ValueError,match='linked cache'):h.cached_source(rec,cache,'license.docx')


def test_manifest_rejects_unreviewed_license_or_runtime_metadata(tmp_path):
 for change in ('license','source','version','member'):
  m=h.load_manifest()
  if change=='license':m['license_file']='license-2015-2022.docx'
  elif change=='source':m['files'][0]['url']='https://example.invalid/terms.docx'
  elif change=='version':next(r for r in m['files'] if r['name'] in h.DLL_NAMES)['file_version']='14.44.35211.0'
  else:next(r for r in m['files'] if r['name'] in h.DLL_NAMES)['cab_member']='other.dll_amd64'
  p=tmp_path/'manifest.json';p.write_text(json.dumps(m))
  with pytest.raises(ValueError):h.load_manifest(p)


def test_build_stages_current_and_previous_notice_without_format_change(tmp_path):
 import build
 directory=tmp_path/'source';directory.mkdir();stage=tmp_path/'stage';(stage/'licenses').mkdir(parents=True)
 m=h.load_manifest()
 for row in m['files']:(directory/row['name']).write_bytes(('fixture '+row['name']).encode())
 with patch.object(h,'verify_source',return_value=(m,{})):
  result=build.vc_runtime(directory,directory/m['license_file'],stage,sorted(h.DLL_NAMES),True)
 assert result['complete'] and len(result['notices'])==2
 for row in result['notices']:
  p=stage/row['file'];assert p.suffix=='.docx'
  original=directory/p.name.removeprefix('Visual-Cpp-redistributable-')
  assert p.read_bytes()==original.read_bytes()
 assert len(list(stage.glob('*.dll')))==4


def test_static_fetch_requires_both_notices_before_publication_and_reuse(tmp_path):
 m=h.load_manifest();binaries={}
 for row in m['files']:
  if row['name'] in h.DLL_NAMES:
   body=bytearray(128);body[:2]=b'MZ';struct.pack_into('<I',body,0x3c,64);body[64:68]=b'PE\0\0';struct.pack_into('<H',body,68,0x8664)
   data=bytes(body)+row['name'].encode();binaries[row['cab_member']]=data
  else:data=('synthetic notice '+row['name']).encode()
  row.update(source(data,row.get('url','https://unused.invalid')))
  if row['name'] in h.DLL_NAMES:row.pop('url')
 outer=b'synthetic outer CAB';inner=b'synthetic inner CAB'
 m['installer']=source(outer);m['cabinets']=[{'offset':0,'members':['a4'],**source(outer)}]
 m['runtime_cab']={'member':'a4',**source(inner)}
 cache=tmp_path/'cache';out=tmp_path/'out'
 def cache_bytes(rec,name,data):
  p=cache/rec['sha256']/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
 cache_bytes(m['installer'],'VC_redist.x64.exe',outer)
 for row in m['files']:
  if row['name'] in h.LICENSE_NAMES:cache_bytes(row,row['name'],('synthetic notice '+row['name']).encode())
 def extract(data,wanted):
  return {'a4':inner} if data==outer else {name:binaries[name] for name in wanted}
 with patch.object(h,'load_manifest',return_value=m),patch.object(h,'extract_cab',side_effect=extract), \
      patch.object(h.subprocess,'run',side_effect=AssertionError('no installer execution')), \
      patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('offline')):
  assert h.fetch(out,cache,offline=True,acknowledge_license=True)==out
  assert {p.name for p in out.iterdir()}==h.DLL_NAMES|h.LICENSE_NAMES|{'vc-runtime-provenance.json'}
  assert h.fetch(out,cache,offline=True,acknowledge_license=True)==out
  (out/'license-2015-2022.docx').write_bytes(b'changed notice')
  with pytest.raises(ValueError,match='integrity mismatch'):h.fetch(out,cache,offline=True,acknowledge_license=True)
  old=next(row for row in m['files'] if row['name']=='license-2015-2022.docx')
  (cache/old['sha256']/old['name']).unlink()
  with pytest.raises(FileNotFoundError):h.fetch(tmp_path/'new-out',cache,offline=True,acknowledge_license=True)
  assert not (tmp_path/'new-out').exists()
