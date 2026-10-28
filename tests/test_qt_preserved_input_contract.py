"""Synthetic offline producer/API contracts; actual raw inputs verified separately."""
import ast,copy,hashlib,json,os,urllib.error,zipfile
from pathlib import Path
from unittest import mock
import pytest
from scripts import qt_preserved_resources as p

def identity():return {'repository':p.REPO,'commit':'a'*40,'run_id':901001,'run_attempt':7,'job':'resources','workflow_path':'.github/workflows/qt-preserved-resource-producer.yml','scope':'GitHub-hosted-producer'}
def metadata():
 i=identity();return {'id':902001,'name':p.artifact_name(i),'digest':'sha256:'+'b'*64,'size_in_bytes':53650000,'expired':False,'expires_at':'2099-01-01T00:00:00Z','workflow_run':{'id':i['run_id'],'head_sha':i['commit']}}
def run():return {'id':901001,'run_attempt':7,'head_sha':'a'*40,'path':identity()['workflow_path'],'repository':{'full_name':p.REPO},'status':'completed','conclusion':'success'}
def job():return {'id':903001,'name':p.JOB_NAME,'run_id':901001,'run_attempt':7,'head_sha':'a'*40,'status':'completed','conclusion':'success'}
def upload_pin():return {'artifact_id':902001,'artifact_digest':'b'*64,'artifact_bytes':53650000,'producer_job_id':903001}
def receipt():
 return {'schema':1,'contract':p.contract()['contract'],'status':'prepared-exact-original-assets-not-publication',
  'producer':identity(),'artifact_name':p.artifact_name(identity()),'assets':p.ASSETS,'historical_origin':p.HISTORY,
  'input_before':p.expected_input(p.contract()),'input_after':p.expected_input(p.contract()),'production_integration_allowed':False,
  'source_archive_handling':'original full corresponding source copied byte-for-byte and fully rehashed, not regenerated',
  'deterministic_resource_reproduction':p.ASSETS[next(n for n in p.ASSETS if n.endswith('.zip'))],
  'source_or_plugin_compiled':False,'historical_container_recreated_or_current':False,'Mac_GUI_or_GPU_verified':False,
  'new_artifact':{'id':902001,'outer_sha256':'b'*64,'bytes':53650000,'expires_at':'2099-01-01T00:00:00Z','producer_job_id':903001}}


def test_reviewed_contract_locks_actual_separate_input_Git_objects_and_original_bytes():
 c=p.contract();assert c['input_commit']!='9e7880e31bb82492225ca1183eb05b5b3fcfb4de';assert c['input_commit']!=p.HISTORY['commit']
 assert c['files']['assets/'+next(n for n in p.ASSETS if n.endswith('.zip'))]['sha256']=='9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441'
 assert c['files']['assets/'+next(n for n in p.ASSETS if n.endswith('.xz'))]['sha256']=='5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd'
 assert c['production_integration_allowed'] is False

@pytest.mark.parametrize('mutation',[
 lambda c:c.update(schema=True),lambda c:c.update(repository='foreign/repo'),lambda c:c.update(input_commit=None),
 lambda c:c.update(input_commit='a'*39),lambda c:c.update(input_tree='A'*40),lambda c:c.update(production_integration_allowed=True),
 lambda c:c['files'].pop(next(iter(c['files']))),lambda c:c.update(asset_names=[]),lambda c:c['manifest'].update(sha256=None),
 lambda c:c['manifest'].update(bytes=True),lambda c:c['files']['assets/'+next(iter(p.ASSETS))].update(sha256='c'*64)])
def test_changed_or_incomplete_input_contract_has_no_fallback(mutation):
 c=copy.deepcopy(p.contract());mutation(c)
 with pytest.raises(RuntimeError):p.validate_contract(c)

@pytest.mark.parametrize('key,value',[('repository','foreign/repo'),('commit','a'*39),('run_id',0),('run_id',True),('run_attempt',True),('run_attempt',0),('job','native'),('workflow_path','.github/workflows/unknown.yml'),('scope','local-production-claim')])
def test_current_identity_requires_exact_namespace_and_typed_actual_ids(key,value):
 i=identity();i[key]=value
 with pytest.raises(RuntimeError):p.validate_identity(i)

def test_local_CLI_cannot_pretend_to_be_hosted_Linux():
 with mock.patch.dict(os.environ,{},clear=True),pytest.raises(RuntimeError,match='actual GitHub-hosted'):p.current_identity()

def test_non2_current_attempt_is_preserved_and_not_replaced_with_historical_origin():
 i=identity();assert '-901001-7' in p.artifact_name(i);assert str(p.HISTORY['run_id']) not in p.artifact_name(i)
 assert p.validate_terminal_origin(receipt(),run(),job(),metadata(),i,upload_pin())

@pytest.mark.parametrize('target,key,value',[
 ('run','id',999),('run','run_attempt',2),('run','head_sha','c'*40),('run','path','.github/workflows/other.yml'),
 ('run','status','in_progress'),('run','conclusion','failure'),('job','id',99),('job','run_id',99),('job','run_attempt',2),
 ('job','head_sha','c'*40),('job','status','in_progress'),('job','conclusion','cancelled'),
 ('meta','id',99),('meta','name','qt-overlay-resources-old'),('meta','digest','sha256:'+'c'*64),('meta','expired',True),
 ('meta','size_in_bytes',0),('meta','expires_at','2000-01-01T00:00:00Z')])
def test_every_stale_failed_or_different_new_producer_field_rejects(target,key,value):
 docs={'run':run(),'job':job(),'meta':metadata()};docs[target][key]=value
 with pytest.raises(RuntimeError):p.validate_terminal_origin(receipt(),docs['run'],docs['job'],docs['meta'],identity(),upload_pin())

@pytest.mark.parametrize('mutation',[lambda r:r.update(production_integration_allowed=True),lambda r:r.update(assets={}),lambda r:r['input_after'].update(input_commit='b'*40),lambda r:r['historical_origin'].update(outer_revalidated_by_new_producer=True),lambda r:r.update(new_artifact=None),lambda r:r['producer'].update(scope='offline-test-fixture')])
def test_receipt_does_not_upgrade_history_or_fixture_or_production_scope(mutation):
 r=copy.deepcopy(receipt());mutation(r)
 with pytest.raises(RuntimeError):p.validate_terminal_origin(r,run(),job(),metadata(),identity(),upload_pin())

def test_403_is_propagated_once_without_token_fallback_or_alternate_endpoint():
 error=urllib.error.HTTPError('https://api.github.com/authorized',403,'Forbidden',{},__import__('io').BytesIO(b'Forbidden'));opener=mock.Mock();opener.open.side_effect=error
 with mock.patch.dict(os.environ,{'GH_TOKEN':'offline-test-only'},clear=True),mock.patch.object(p.urllib.request,'build_opener',return_value=opener),pytest.raises(RuntimeError,match='HTTP 403'):
  p.github_read('/repos/'+p.REPO+'/actions/artifacts/902001')
 assert opener.open.call_count==1

def test_unapproved_endpoint_fails_before_network():
 with mock.patch.object(p.urllib.request,'build_opener') as opener,pytest.raises(RuntimeError):p.github_read('/repos/foreign/repo/actions/artifacts/1')
 opener.assert_not_called()

def test_capture_records_actual_new_metadata_while_retaining_in_progress_boundary(tmp_path):
 out=tmp_path/'out';(out/'reports').mkdir(parents=True);(out/'assets').mkdir();r=receipt();r['new_artifact']=None;p.write_json(out/'reports/preparation.json',r)
 j=job();j['status']='in_progress';j['conclusion']=None;actualrun=run();actualrun['status']='in_progress';actualrun['conclusion']=None
 docs=[metadata(),actualrun,{'total_count':1,'jobs':[j]}];paths=[]
 def api(path):
  paths.append(path);doc=docs[len(paths)-1];return doc,(json.dumps(doc)+'\n').encode()
 with mock.patch.object(p,'verify_inputs',return_value=p.expected_input(p.contract())),mock.patch.object(p,'file_record',side_effect=lambda name:p.ASSETS[Path(name).name]):
  recorded=p.capture_upload(out,p.contract(),tmp_path,identity(),902001,'b'*64,read=api)
 assert len(paths)==3 and all(str(p.HISTORY['artifact_id']) not in path and str(p.HISTORY['run_id']) not in path for path in paths)
 assert recorded['new_artifact']['id']==902001 and recorded['new_artifact']['producer_job_id']==903001
 assert recorded['actual_producer_job']['status']=='in_progress' and 'terminal success' in recorded['upload_capture_scope']
 with pytest.raises(RuntimeError):p.validate_terminal_origin(recorded,actualrun,j,metadata(),identity(),upload_pin())


def test_offline_fixture_cannot_capture_a_real_upload(tmp_path):
 i=identity();i['scope']='offline-test-fixture'
 with pytest.raises(RuntimeError,match='Offline fixtures'):p.capture_upload(tmp_path,p.contract(),tmp_path,i,902001,'b'*64)

def test_new_delivery_requires_whole_outer_hash_before_extracting(tmp_path):
 archive=tmp_path/'outer.zip';archive.write_bytes(b'wrong bytes')
 with mock.patch.object(p.archives,'extract_archive') as extract,pytest.raises(RuntimeError,match='Whole new outer'):
  p.verify_new_delivery(archive,tmp_path/'out',receipt(),run(),job(),metadata(),identity(),upload_pin())
 extract.assert_not_called()

def test_correct_outer_cannot_introduce_an_extra_member(tmp_path):
 archive=tmp_path/'outer.zip'
 with zipfile.ZipFile(archive,'w') as z:z.writestr('unlisted.txt',b'not an original asset')
 meta=metadata();meta['size_in_bytes']=archive.stat().st_size;meta['digest']='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()
 r=receipt();r['new_artifact'].update(bytes=meta['size_in_bytes'],outer_sha256=meta['digest'][7:])
 with pytest.raises(RuntimeError,match='two-asset'):p.verify_new_delivery(archive,tmp_path/'out',r,run(),job(),meta,identity(),{**upload_pin(),'artifact_digest':meta['digest'][7:],'artifact_bytes':meta['size_in_bytes']})

@pytest.mark.parametrize('expiry',[None,'','broken','2099-01-01T00:00:00'])
def test_new_artifact_expiry_must_be_explicit_aware_and_valid(expiry):
 meta=metadata();meta['expires_at']=expiry
 with pytest.raises(RuntimeError):p.validate_artifact(meta,identity(),902001,'b'*64)

@pytest.mark.parametrize('aid,digest',[(None,'b'*64),(True,'b'*64),(902001,None),(902001,'bad')])
def test_incomplete_upload_identity_fails_before_any_API(aid,digest,tmp_path):
 read=mock.Mock()
 with pytest.raises(RuntimeError):p.capture_upload(tmp_path,p.contract(),tmp_path,identity(),aid,digest,read=read)
 read.assert_not_called()

@pytest.mark.parametrize('field,value',[('input_before',{}),('deterministic_resource_reproduction',{}),('source_or_plugin_compiled',True),('Mac_GUI_or_GPU_verified',True),('status','passed'),('schema',True)])
def test_preparation_evidence_cannot_change_fixed_inputs_or_claims(field,value):
 r=copy.deepcopy(receipt());r[field]=value
 with pytest.raises(RuntimeError):p.validate_terminal_origin(r,run(),job(),metadata(),identity(),upload_pin())

@pytest.mark.parametrize('old_id',[12624219589,12688107623,12735996442])
def test_old_containers_cannot_be_renamed_into_a_new_upload_claim(old_id):
 meta=metadata();meta['id']=old_id
 with pytest.raises(RuntimeError,match='NEW artifact'):p.validate_artifact(meta,identity(),old_id,'b'*64)

@pytest.mark.parametrize('kind',['artifact','job'])
def test_self_consistent_receipt_and_API_cannot_replace_independent_upload_pins(kind):
 r=receipt();meta=metadata();j=job()
 if kind=='artifact':r['new_artifact']['id']=999001;meta['id']=999001
 else:r['new_artifact']['producer_job_id']=999002;j['id']=999002
 with pytest.raises(RuntimeError,match='independent actual upload/job pins'):p.validate_terminal_origin(r,run(),j,meta,identity(),upload_pin())

def test_unrelated_successful_job_name_cannot_be_the_producer():
 j=job();j['name']='Unrelated successful job'
 with pytest.raises(RuntimeError):p.validate_terminal_origin(receipt(),run(),j,metadata(),identity(),upload_pin())

def test_capture_failure_retains_reported_upload_and_raw_error_without_retry(tmp_path):
 out=tmp_path/'out';(out/'reports').mkdir(parents=True);(out/'assets').mkdir();r=receipt();r['new_artifact']=None;p.write_json(out/'reports/preparation.json',r)
 api=mock.Mock(side_effect=p.APIReadFailure(403,b'{"message":"Forbidden"}'))
 with mock.patch.object(p,'verify_inputs',return_value=p.expected_input(p.contract())),mock.patch.object(p,'file_record',side_effect=lambda name:p.ASSETS[Path(name).name]),pytest.raises(RuntimeError,match='HTTP 403'):
  p.capture_upload(out,p.contract(),tmp_path,identity(),902001,'b'*64,read=api)
 assert api.call_count==1 and (out/'reports/actual-artifact-failure.raw').read_bytes()==b'{"message":"Forbidden"}'
 reported=p.read_json(out/'reports/upload-reported-by-action.json');assert reported['artifact_id']==902001
 assert not (out/'reports/producer-receipt.json').exists()
