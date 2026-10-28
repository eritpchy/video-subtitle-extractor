"""Synthetic contracts: no Microsoft binaries, license text or real network."""
import copy, hashlib, io, json, pathlib, struct, tempfile, unittest, urllib.error
from unittest.mock import patch
import pytest
import vc_runtime_helper as h


def source(data):
 return {'bytes':len(data),'sha256':h.digest(data),'url':'https://download.visualstudio.microsoft.com/download/pr/test/VC_redist.x64.exe'}


def fixture(root):
 root.mkdir(exist_ok=True);m=copy.deepcopy(h.load_manifest());binaries={}
 for row in m['files']:
  if row['name'] in h.DLL_NAMES:
   data=bytearray(128);data[:2]=b'MZ';struct.pack_into('<I',data,0x3c,64);data[64:68]=b'PE\0\0';struct.pack_into('<H',data,68,0x8664)
   data=bytes(data)+row['name'].encode();row.update(bytes=len(data),sha256=h.digest(data));binaries[row['cab_member']]=data
  else:data=pathlib.Path(h.__file__).with_name(row['source_file']).read_bytes()
  (root/row['name']).write_bytes(data)
 return m,binaries


def test_five_dll_hashes_machine_and_separate_attribution(tmp_path):
 m,_=fixture(tmp_path)
 with patch.object(h,'load_manifest',return_value=m):
  _,values=h.verify_source(tmp_path,tmp_path/h.NOTICE_FILE);assert set(values)==h.DLL_NAMES|{h.NOTICE_FILE}
  p=tmp_path/'concrt140.dll';p.write_bytes(b'changed')
  with pytest.raises(ValueError,match='integrity mismatch'):h.verify_source(tmp_path,tmp_path/h.NOTICE_FILE)


def test_wrong_machine_rejected_even_with_matching_digest(tmp_path):
 m,_=fixture(tmp_path);row=next(r for r in m['files'] if r['name']=='concrt140.dll');p=tmp_path/row['name']
 data=bytearray(p.read_bytes());struct.pack_into('<H',data,68,0x14c);p.write_bytes(data);row['sha256']=h.digest(data)
 with patch.object(h,'load_manifest',return_value=m),pytest.raises(ValueError,match='x64 PE'):h.verify_source(tmp_path,tmp_path/h.NOTICE_FILE)


def test_assertion_required_before_any_read_or_network(tmp_path):
 with patch.object(h,'load_manifest',side_effect=AssertionError('no input read')),patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('no network')):
  with pytest.raises(PermissionError):h.fetch(tmp_path/'out',tmp_path/'cache')


def test_offline_never_downloads(tmp_path):
 with patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')):
  with pytest.raises(FileNotFoundError):h.fetch(tmp_path/'out',tmp_path/'cache',offline=True,acknowledge_license=True)


def test_bad_archive_and_cab_fail():
 with pytest.raises(ValueError):h.checked(b'bad',dict(bytes=3,sha256='0'*64))
 with pytest.raises(ValueError):h.cab_files(b'bad',[])


def test_committed_manifest_retains_original_runtime_pins_and_review_boundary():
 m=h.load_manifest();assert m['version']=='14.51.36247.0' and m['architecture']=='x64' and m['schema']==2
 assert m['installer']['sha256']=='843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c'
 expected={'msvcp140.dll':'7c26614e1d733892c2deac7e245ce115504b1d80592dd0a01b08e3e5a55f89ca','msvcp140_1.dll':'206c931bf90fdad8816de3b5e2ef80b2bcaa9406c89ecc05fe6fddffe251e982','vcruntime140.dll':'d1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7','vcruntime140_1.dll':'a7146c08f89fe5b04541ab507cdb59ff7b44534d4ba3c668a426c6450a03434e','concrt140.dll':'54716f0738af891f283d213b5c8d11b25896bb8ee3097d301eae718560cf974e'}
 assert {r['name']:r['sha256'] for r in m['files'] if r['name'] in h.DLL_NAMES}==expected
 assert {r['name'] for r in m['files']}==h.DLL_NAMES|{h.NOTICE_FILE}
 assert m['license_file']==h.NOTICE_FILE and len(m['historical_document_pins'])==2
 assert not m['licensing_review']['final_publication_authorized']
 assert m['licensing_review']['status']=='applicable-terms-not-independently-verified'
 row=next(r for r in m['files'] if r['name']==h.NOTICE_FILE);assert h.local_notice(row) and 'url' not in row


def test_content_addressed_cache_revalidates_hits(tmp_path):
 (tmp_path/'VC_redist.x64.exe').write_bytes(b'old runtime');rec=source(b'new runtime')
 with patch.object(h.urllib.request,'urlopen',return_value=io.BytesIO(b'new runtime')) as request:
  assert h.cached_source(rec,tmp_path,'VC_redist.x64.exe')==b'new runtime';request.assert_called_once_with(rec['url'],timeout=120)
 with patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')):
  assert h.cached_source(rec,tmp_path,'VC_redist.x64.exe',offline=True)==b'new runtime'
  (tmp_path/rec['sha256']/'VC_redist.x64.exe').write_bytes(b'tampered')
  with pytest.raises(ValueError,match='integrity mismatch'):h.cached_source(rec,tmp_path,'VC_redist.x64.exe',offline=True)
 assert (tmp_path/'VC_redist.x64.exe').read_bytes()==b'old runtime'


@pytest.mark.parametrize('name',['license.docx','license-2015-2022.docx',h.NOTICE_FILE,'../escape'])
def test_cache_cannot_fetch_old_or_replacement_license_content(tmp_path,name):
 with patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')):
  with pytest.raises(ValueError):h.cached_source(source(b'x'),tmp_path,name)


def test_runtime_installer_403_has_no_fallback_or_output(tmp_path):
 m=h.load_manifest();error=urllib.error.HTTPError(m['installer']['url'],403,'Forbidden',{},None)
 with patch.object(h.urllib.request,'urlopen',side_effect=error) as request:
  with pytest.raises(urllib.error.HTTPError):h.fetch(tmp_path/'out',tmp_path/'cache',acknowledge_license=True)
  assert request.call_count==1 and request.call_args.args[0]==m['installer']['url']
 assert not (tmp_path/'out').exists()


def test_cache_rejects_unsafe_digest_and_symlink(tmp_path):
 rec=source(b'x')
 with pytest.raises(ValueError):h.cached_source({**rec,'sha256':'../escape'},tmp_path,'VC_redist.x64.exe')
 target=tmp_path/'target';target.mkdir();cache=tmp_path/'linked';cache.symlink_to(target,target_is_directory=True)
 with pytest.raises(ValueError,match='linked cache'):h.cached_source(rec,cache,'VC_redist.x64.exe')


def test_manifest_rejects_bad_sources_runtime_and_false_legal_claims(tmp_path):
 for change in ('name','source','version','member','missing','extra','license','claim','installer'):
  m=h.load_manifest();dll=next(r for r in m['files'] if r['name'] in h.DLL_NAMES);notice=next(r for r in m['files'] if r['name']==h.NOTICE_FILE)
  if change=='name':dll['name']='../escape'
  elif change=='source':notice['url']='https://visualstudio.microsoft.com/blocked.docx'
  elif change=='version':dll['file_version']='14.44.35211.0'
  elif change=='member':dll['cab_member']='other.dll_amd64'
  elif change=='missing':m['files'].remove(dll)
  elif change=='extra':m['files'].append({'name':'unknown_vendor.dll'})
  elif change=='license':m['license_file']='license.docx'
  elif change=='claim':m['licensing_review']['final_publication_authorized']=True
  else:m['installer']['url']='https://visualstudio.microsoft.com/blocked.docx'
  p=tmp_path/'manifest.json';p.write_text(json.dumps(m))
  with pytest.raises(ValueError):h.load_manifest(p)


def test_build_stages_five_dlls_and_accurate_attribution(tmp_path):
 import build
 directory=tmp_path/'source';m,_=fixture(directory);stage=tmp_path/'stage';(stage/'licenses').mkdir(parents=True)
 required=sorted(h.DLL_NAMES-{'concrt140.dll'})
 with patch.object(h,'load_manifest',return_value=m):result=build.vc_runtime(directory,directory/h.NOTICE_FILE,stage,required,True)
 assert result['complete'] and len(result['notices'])==1 and not result['licensing_review']['final_publication_authorized']
 assert {p.name for p in stage.glob('*.dll')}==h.DLL_NAMES
 for row in result['notices']:assert (stage/row['file']).read_bytes()==(directory/h.NOTICE_FILE).read_bytes()


def test_build_rejects_unknown_dll_before_copying(tmp_path):
 import build
 directory=tmp_path/'source';m,_=fixture(directory);stage=tmp_path/'stage';stage.mkdir()
 with patch.object(h,'load_manifest',return_value=m),pytest.raises(ValueError,match='not reviewed: unknown_vendor.dll'):build.vc_runtime(directory,directory/h.NOTICE_FILE,stage,['unknown_vendor.dll'],True)
 assert not list(stage.iterdir())


def test_fetch_only_installer_ignores_legacy_documents_and_verifies_output(tmp_path):
 m,binaries=fixture(tmp_path/'fixture');outer=b'synthetic outer CAB';inner=b'synthetic inner CAB'
 m['installer']=source(outer);m['cabinets']=[{'offset':0,'members':['a4'],**source(outer)}];m['runtime_cab']={'member':'a4',**source(inner)}
 cache=tmp_path/'cache';cache.mkdir();out=tmp_path/'out'
 for name in ('license.docx','license-2015-2022.docx'):(cache/name).write_bytes(b'never adopted')
 def extract(data,wanted):return {'a4':inner} if data==outer else {name:binaries[name] for name in wanted}
 with patch.object(h,'load_manifest',return_value=m),patch.object(h,'extract_cab',side_effect=extract),patch.object(h.subprocess,'run',side_effect=AssertionError('no execution')),patch.object(h.urllib.request,'urlopen',return_value=io.BytesIO(outer)) as request:
  assert h.fetch(out,cache,acknowledge_license=True)==out
  assert request.call_count==1 and request.call_args.args[0]==m['installer']['url']
  assert {p.name for p in out.iterdir()}==h.DLL_NAMES|{h.NOTICE_FILE,'vc-runtime-provenance.json'}
  assert h.fetch(out,cache,offline=True,acknowledge_license=True)==out and request.call_count==1
  (out/h.NOTICE_FILE).write_bytes(b'changed attribution')
  with pytest.raises(ValueError,match='integrity mismatch'):h.fetch(out,cache,offline=True,acknowledge_license=True)


def test_local_attribution_failure_stops_before_network(tmp_path):
 m=h.load_manifest();next(r for r in m['files'] if r['name']==h.NOTICE_FILE)['sha256']='0'*64
 with patch.object(h,'load_manifest',return_value=m),patch.object(h.urllib.request,'urlopen',side_effect=AssertionError('no network')):
  with pytest.raises(ValueError,match='integrity mismatch'):h.fetch(tmp_path/'out',tmp_path/'cache',acknowledge_license=True)
 assert not (tmp_path/'out').exists()
