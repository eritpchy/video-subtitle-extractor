#!/usr/bin/env python3
"""Explicit preserved Qt Git-input producer. No old-artifact request or fallback.
Pure validators support offline review; CLI preparation/capture require hosted Linux.
"""
from __future__ import annotations
import argparse,datetime,hashlib,json,os,re,shutil,stat,subprocess,sys,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts import macos_qt_overlay as overlay
from scripts import macos_ax_archive as archives
from scripts import build_macos_qt_overlay_resources as builder
REPO='eritpchy/video-subtitle-extractor'
CONTRACT_PATH=ROOT/'native/qt/preserved-input-contract.json'
JOB_NAME='Preserve exact Qt Git inputs (not publication)'
ARTIFACT_PREFIX='qt-overlay-resources-'
ASSETS={
 'vse-qt6.12.0-macos-arm64-ax772484-ps6.zip':{'bytes':2547078,'sha256':'9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441'},
 'vse-qtbase6.12.0-ax772484-ps6-source.tar.xz':{'bytes':51103812,'sha256':'5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd'}}
HISTORY={'repository':REPO,'run_id':40391756697,'run_attempt':2,'job_id':121842501324,
 'commit':'8f08e7f6887a950f75cb7967f1f80e344935ecaa','artifact_id':12688107623,
 'outer_sha256':'f1e47e5ee2e765dc0661d5ae5288435ec5da181d49f998bdd6e2f263580fef65',
 'outer_revalidated_by_new_producer':False,'historical_expiry':'2026-10-27T20:55:03Z'}
PROVENANCE={'provenance/original8f-attempt2.json','provenance/original8f-qt-job.json',
 'provenance/original-resource-build-receipt.json','provenance/original-resource-preparation.json'}

def need(value,message):
 if not value:raise RuntimeError(message)

def sha(data):return hashlib.sha256(data).hexdigest()
def git_id(kind,data):return hashlib.sha1(kind.encode()+b' '+str(len(data)).encode()+b'\0'+data).hexdigest()
def hex40(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{40}',value)
def hex64(value):return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value)
def positive(value):return type(value) is int and value>0

def read_json(path):
 def pairs(rows):
  result={}
  for name,value in rows:need(name not in result,'Duplicate JSON key');result[name]=value
  return result
 raw=Path(path).read_bytes();need(len(raw)<=4*1024**2,'Oversized JSON');value=json.loads(raw,object_pairs_hook=pairs);need(isinstance(value,dict),'JSON object required');return value

def write_json(path,value):
 with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,ensure_ascii=True);f.write('\n')

def file_record(path):
 path=Path(path);s=path.lstat();need(stat.S_ISREG(s.st_mode) and not path.is_symlink() and s.st_nlink==1,'Plain unlinked file required')
 h=hashlib.sha256()
 with path.open('rb') as f:
  for chunk in iter(lambda:f.read(1024**2),b''):h.update(chunk)
 return {'bytes':s.st_size,'sha256':h.hexdigest()}

def git(root,*args):
 env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
 env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_NO_REPLACE_OBJECTS='1',GIT_TERMINAL_PROMPT='0',GIT_OPTIONAL_LOCKS='0')
 return subprocess.check_output(['git','--no-replace-objects','-c','core.fsmonitor=false','-C',str(root),*args],env=env,stderr=subprocess.PIPE)

def validate_contract(c):
 need(set(c)=={'schema','contract','repository','input_commit','input_tree','manifest','files','asset_names','production_integration_allowed','scope'},'Exact contract fields required')
 need(type(c['schema']) is int and c['schema']==1 and c['contract']=='preserved-original-Qt-Git-input-v1' and c['repository']==REPO,'Wrong input contract')
 need(hex40(c['input_commit']) and hex40(c['input_tree']),'Explicit full input Git commit/tree required')
 need(c['production_integration_allowed'] is False,'Production admission must remain false')
 expected={'assets/'+name for name in ASSETS}|PROVENANCE
 need(isinstance(c['files'],dict) and set(c['files'])==expected and c['asset_names']==sorted(ASSETS),'Exact original input closure required')
 for name,row in {**c['files'],'INPUT-MANIFEST.json':c['manifest']}.items():
  need(set(row)=={'bytes','sha256'} and positive(row['bytes']) and row['bytes']<=64*1024**2 and hex64(row['sha256']),'Missing exact input byte/hash pin')
 for name,expected_row in ASSETS.items():need(c['files']['assets/'+name]==expected_row,'Original resource/source byte pins cannot change')
 return c

def contract():return validate_contract(read_json(CONTRACT_PATH))

def tree_files(root,oid):
 raw=git(root,'cat-file','tree',oid);need(git_id('tree',raw)==oid,'Raw Git tree hash mismatch');result={};position=0
 while position<len(raw):
  space=raw.index(b' ',position);end=raw.index(b'\0',space+1);mode=raw[position:space].decode();name=raw[space+1:end].decode();child=raw[end+1:end+21].hex();position=end+21
  need(name and name not in ('.','..') and '/' not in name and '\\' not in name and len(child)==40,'Unsafe Git tree entry')
  if mode=='40000':
   for relative,value in tree_files(root,child).items():result[name+'/'+relative]=value
  else:need(mode=='100644','Only regular nonexecutable Git input files accepted');result[name]=child
 return result

def verify_inputs(root,c):
 validate_contract(c);root=Path(root);need(root.is_dir() and not root.is_symlink(),'Plain input checkout required')
 need(git(root,'rev-parse','HEAD').decode().strip()==c['input_commit'],'Input HEAD differs from explicit commit')
 commit=git(root,'cat-file','commit',c['input_commit']);need(git_id('commit',commit)==c['input_commit'] and commit.split(b'\n',1)[0]==b'tree '+c['input_tree'].encode(),'Input commit/tree object mismatch')
 need(not git(root,'status','--porcelain=v1','--untracked-files=all'),'Input checkout is dirty')
 expected={**c['files'],'INPUT-MANIFEST.json':c['manifest']};tree=tree_files(root,c['input_tree']);need(set(tree)==set(expected),'Wrong authenticated Git source closure')
 actual=set()
 for path in root.rglob('*'):
  if '.git' in path.relative_to(root).parts:continue
  need(not path.is_symlink(),'Linked input path')
  if path.is_file():actual.add(path.relative_to(root).as_posix())
 need(actual==set(expected),'Unexpected/missing working input file')
 for name,row in expected.items():
  need(file_record(root/name)==row,'Input bytes differ from contract: '+name)
  data=(root/name).read_bytes();need(git_id('blob',data)==tree[name],'Working bytes do not belong to authenticated Git tree')
 manifest=read_json(root/'INPUT-MANIFEST.json')
 need(set(manifest)=={'schema','kind','files','historical_origin','scope','production_integration_allowed'} and type(manifest['schema']) is int and manifest['schema']==1
      and manifest['kind']=='fixed-original-Qt-assets-independent-Git-input' and manifest['files']==c['files']
      and manifest['historical_origin']==HISTORY and manifest['production_integration_allowed'] is False and manifest['scope']==c['scope'],'Wrong preserved input manifest')
 run=read_json(root/'provenance/original8f-attempt2.json');job=read_json(root/'provenance/original8f-qt-job.json')
 for obj,fields in [(run,{'id':HISTORY['run_id'],'run_attempt':2,'head_sha':HISTORY['commit'],'status':'completed','conclusion':'success'}),
                    (job,{'id':HISTORY['job_id'],'run_id':HISTORY['run_id'],'run_attempt':2,'head_sha':HISTORY['commit'],'status':'completed','conclusion':'success','name':'qt-overlay-resources / resources'})]:
  need(all(type(obj.get(k)) is type(v) and obj.get(k)==v for k,v in fields.items()),'Wrong historical successful origin')
 for name in ['original-resource-build-receipt.json','original-resource-preparation.json']:
  doc=read_json(root/'provenance'/name);need(doc['production_integration_allowed'] is False and doc['resource_manifest_sha256']==overlay.load_policy()['resource_manifest_sha256'],'Historical scope or resource manifest differs')
  for key,asset in [('resource_asset',next(n for n in ASSETS if n.endswith('.zip'))),('source_asset',next(n for n in ASSETS if n.endswith('.xz')))]:need(doc[key]=={'name':asset,**ASSETS[asset]},'Historical exact asset identity differs')
 return {'input_commit':c['input_commit'],'input_tree':c['input_tree'],'contract_sha256':sha(CONTRACT_PATH.read_bytes()),'files':expected,'Git_objects_and_all_file_bytes_verified':True}

def expected_input(c):
 validate_contract(c)
 return {'input_commit':c['input_commit'],'input_tree':c['input_tree'],'contract_sha256':sha(CONTRACT_PATH.read_bytes()),
         'files':{**c['files'],'INPUT-MANIFEST.json':c['manifest']},'Git_objects_and_all_file_bytes_verified':True}

def validate_preparation(receipt,c,i):
 validate_identity(i);expected=expected_input(c)
 need(receipt.get('schema')==1 and type(receipt.get('schema')) is int and receipt.get('contract')==c['contract']
      and receipt.get('status')=='prepared-exact-original-assets-not-publication' and receipt.get('producer')==i
      and receipt.get('artifact_name')==artifact_name(i) and receipt.get('input_before')==expected and receipt.get('input_after')==expected
      and receipt.get('assets')==ASSETS and receipt.get('historical_origin')==HISTORY
      and receipt.get('deterministic_resource_reproduction')==ASSETS[next(n for n in ASSETS if n.endswith('.zip'))]
      and receipt.get('source_archive_handling')=='original full corresponding source copied byte-for-byte and fully rehashed, not regenerated'
      and receipt.get('source_or_plugin_compiled') is False and receipt.get('historical_container_recreated_or_current') is False
      and receipt.get('production_integration_allowed') is False and receipt.get('Mac_GUI_or_GPU_verified') is False,
      'Incomplete or changed fixed-input preparation receipt')
 return True

def validate_identity(i):
 need(set(i)=={'repository','commit','run_id','run_attempt','job','workflow_path','scope'},'Exact producer identity required')
 need(i['repository']==REPO and hex40(i['commit']) and positive(i['run_id']) and positive(i['run_attempt']) and i['job']=='resources','Invalid producer identity')
 need(i['workflow_path'] in ('.github/workflows/qt-preserved-resource-producer.yml','.github/workflows/qt-overlay-resources.yml','.github/workflows/release.yml','.github/workflows/native-ocr.yml','.github/workflows/python-source.yml'),'Unreviewed producer workflow caller')
 need(i['scope'] in ('GitHub-hosted-producer','offline-test-fixture'),'Producer identity scope required');return i

def current_identity():
 need(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted' and os.environ.get('RUNNER_OS')=='Linux','CLI requires actual GitHub-hosted Linux')
 workflow=os.environ.get('GITHUB_WORKFLOW_REF','');prefix=REPO+'/'
 need(workflow.startswith(prefix) and '@refs/heads/' in workflow,'Actual same-repository branch workflow ref required')
 values=[os.environ.get(k,'') for k in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT')];need(all(re.fullmatch('[1-9][0-9]*',v) for v in values),'Actual positive run/attempt required')
 return validate_identity({'repository':os.environ.get('GITHUB_REPOSITORY'),'commit':os.environ.get('GITHUB_SHA'),'run_id':int(values[0]),'run_attempt':int(values[1]),'job':os.environ.get('GITHUB_JOB'),'workflow_path':workflow[len(prefix):].split('@',1)[0],'scope':'GitHub-hosted-producer'})

def artifact_name(i):return ARTIFACT_PREFIX+i['commit']+'-'+str(i['run_id'])+'-'+str(i['run_attempt'])

def prepare(root,output,c,identity):
 validate_identity(identity);before=verify_inputs(root,c);output=Path(output).absolute();root=Path(root).resolve()
 need(not output.exists() and not output.is_symlink() and not output.resolve().is_relative_to(root) and not output.resolve().is_relative_to(ROOT),'New output outside immutable input and driver required')
 output.mkdir(parents=True);reports=output/'reports';reports.mkdir();failure=None
 try:
  assets=output/'assets';assets.mkdir();policy=overlay.load_policy()
  for key,name in [('resource',next(n for n in ASSETS if n.endswith('.zip'))),('source_resource',next(n for n in ASSETS if n.endswith('.xz')))]:
   need({k:policy[key][k] for k in ('bytes','sha256')}==ASSETS[name] and policy[key]['name']==name and policy['production_integration_allowed'] is False,'Frozen application policy differs')
   shutil.copyfile(root/'assets'/name,assets/name);need(file_record(assets/name)==ASSETS[name],'Copied original bytes changed')
  resource=overlay.prepare_resource(assets/policy['resource']['name'],output/'verified-resource',policy=policy);overlay._validate_resource(resource)
  files={p.relative_to(resource.root).as_posix():p.read_bytes() for p in resource.root.rglob('*') if p.is_file()}
  rebuilt=output/'resource-reproduced.zip';builder.write_resource_zip(rebuilt,files);need(file_record(rebuilt)==ASSETS[policy['resource']['name']],'Deterministic resource ZIP reproduction differs')
  after=verify_inputs(root,c);need(after==before,'Original Git inputs changed')
  receipt={'schema':1,'contract':c['contract'],'status':'prepared-exact-original-assets-not-publication','producer':identity,
   'artifact_name':artifact_name(identity),'input_before':before,'input_after':after,'assets':ASSETS,'historical_origin':HISTORY,
   'source_archive_handling':'original full corresponding source copied byte-for-byte and fully rehashed, not regenerated',
   'deterministic_resource_reproduction':file_record(rebuilt),'source_or_plugin_compiled':False,'historical_container_recreated_or_current':False,
   'production_integration_allowed':False,'Mac_GUI_or_GPU_verified':False,'new_artifact':None}
  write_json(reports/'preparation.json',receipt);return receipt
 except Exception as error:
  failure=error;write_json(reports/'failure.json',{'status':'failed','exception':type(error).__name__,'error':str(error),'producer':identity,'production_integration_allowed':False});raise

class APIReadFailure(RuntimeError):
 def __init__(self,code,body):
  super().__init__('GitHub API failed: HTTP '+str(code)+'; no retry or fallback');self.code=code;self.body=body

class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def github_read(path):
 need(re.fullmatch(r'/repos/eritpchy/video-subtitle-extractor/actions/(artifacts/[1-9][0-9]*|runs/[1-9][0-9]*/attempts/[1-9][0-9]*(/jobs\?per_page=100)?)',path),'Unapproved API endpoint')
 token=os.environ.get('GH_TOKEN','');need(token,'Read-only job token required; no authentication fallback')
 request=urllib.request.Request('https://api.github.com'+path,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','User-Agent':'vse-qt-preserved-input-producer'})
 try:
  with urllib.request.build_opener(NoRedirect()).open(request,timeout=60) as reply:raw=reply.read(4*1024**2+1)
 except urllib.error.HTTPError as error:
  body=error.read(4*1024**2);body=body.replace(token.encode(),b'[REDACTED]') if token else body
  raise APIReadFailure(error.code,body) from None
 need(len(raw)<=4*1024**2,'Oversized API response');return json.loads(raw),raw

def validate_artifact(meta,i,aid,digest):
 need(positive(aid) and aid not in (12624219589,12688107623,12735996442) and hex64(digest),'Actual NEW artifact ID/digest required')
 need(type(meta.get('id')) is int and meta.get('id')==aid and meta.get('name')==artifact_name(i)
      and meta.get('digest')=='sha256:'+digest and positive(meta.get('size_in_bytes')) and meta['size_in_bytes']<64*1024**2
      and meta.get('expired') is False and meta.get('workflow_run',{}).get('id')==i['run_id'] and meta['workflow_run'].get('head_sha')==i['commit'],'New artifact identity differs from actual producer')
 try:expiry=datetime.datetime.fromisoformat(meta['expires_at'].replace('Z','+00:00'))
 except (KeyError,TypeError,ValueError,AttributeError):raise RuntimeError('New artifact expiry missing or malformed') from None
 need(expiry.tzinfo is not None and expiry>datetime.datetime.now(datetime.timezone.utc),'New artifact expired or timezone missing')

def capture_upload(output,c,root,identity,aid,digest,read=github_read):
 validate_identity(identity);need(identity['scope']=='GitHub-hosted-producer','Offline fixtures cannot claim a real upload')
 need(positive(aid) and aid not in (12624219589,12688107623,12735996442) and hex64(digest),'Actual NEW artifact ID/digest required before API access')
 output=Path(output);receipt=read_json(output/'reports/preparation.json');validate_preparation(receipt,c,identity);need(receipt['input_after']==verify_inputs(root,c),'Preparation/current producer input mismatch')
 for name,row in ASSETS.items():need(file_record(output/'assets'/name)==row,'Upload input asset changed')
 write_json(output/'reports/upload-reported-by-action.json',{'producer':identity,'artifact_id':aid,'artifact_digest':digest,'scope':'Uploader reported; not yet independently verified by API; no terminal success'})
 base='/repos/'+REPO+'/actions/';api_results={}
 for label,path in [('artifact',base+'artifacts/'+str(aid)),('attempt',base+'runs/'+str(identity['run_id'])+'/attempts/'+str(identity['run_attempt'])),('jobs',base+'runs/'+str(identity['run_id'])+'/attempts/'+str(identity['run_attempt'])+'/jobs?per_page=100')]:
  try:doc,raw=read(path)
  except Exception as error:
   write_json(output/'reports'/('actual-'+label+'-failure.json'),{'endpoint':path,'exception':type(error).__name__,'error':str(error),'http_status':getattr(error,'code',None),'retry_or_fallback':False})
   if isinstance(error,APIReadFailure):(output/'reports'/('actual-'+label+'-failure.raw')).write_bytes(error.body)
   raise
  api_results[label]=doc;(output/'reports'/('actual-'+label+'.json')).write_bytes(raw)
 meta=api_results['artifact'];validate_artifact(meta,identity,aid,digest);run=api_results['attempt']
 need(run.get('id')==identity['run_id'] and type(run.get('run_attempt')) is int and run.get('run_attempt')==identity['run_attempt'] and run.get('head_sha')==identity['commit'] and run.get('path')==identity['workflow_path'] and run.get('repository',{}).get('full_name')==REPO,'Actual run/attempt/workflow mismatch')
 jobs=api_results['jobs'];need(jobs.get('total_count')==len(jobs.get('jobs',[])) and len(jobs['jobs'])<=100,'Incomplete job page')
 selected=[j for j in jobs['jobs'] if j.get('name')==JOB_NAME or j.get('name','').endswith(' / '+JOB_NAME)]
 need(len(selected)==1,'Actual unique producer job required');job=selected[0]
 need(positive(job.get('id')) and job.get('run_id')==identity['run_id'] and job.get('run_attempt')==identity['run_attempt'] and job.get('head_sha')==identity['commit'] and job.get('status')=='in_progress','Current producer job identity/status mismatch')
 receipt['new_artifact']={'id':aid,'name':meta['name'],'outer_sha256':digest,'bytes':meta['size_in_bytes'],'expires_at':meta['expires_at'],'producer_job_id':job['id'],'api_metadata':meta}
 receipt['upload_capture_scope']='Actual upload metadata while producer job is in_progress; terminal success MUST be independently required by consumer'
 receipt['observed_attempt_status']=run.get('status');receipt['actual_producer_job']=job
 need(receipt['input_after']==verify_inputs(root,c),'Input changed during upload capture')
 write_json(output/'reports/producer-receipt.json',receipt);return receipt

def validate_terminal_origin(receipt,run,job,meta,expected,upload_pin):
 validate_identity(expected);validate_preparation(receipt,contract(),expected)
 need(receipt.get('producer')==expected and expected.get('scope')=='GitHub-hosted-producer','Exact current producer identity required')
 selected=receipt.get('new_artifact');need(isinstance(selected,dict),'No actual uploaded artifact record')
 need(isinstance(upload_pin,dict) and set(upload_pin)=={'artifact_id','artifact_digest','artifact_bytes','producer_job_id'}
      and positive(upload_pin['artifact_id']) and positive(upload_pin['artifact_bytes']) and positive(upload_pin['producer_job_id']) and hex64(upload_pin['artifact_digest']), 'Independent upload/job pins required')
 need(selected['id']==upload_pin['artifact_id'] and selected['outer_sha256']==upload_pin['artifact_digest']
      and selected['bytes']==upload_pin['artifact_bytes'] and selected['producer_job_id']==upload_pin['producer_job_id'], 'Receipt differs from independent actual upload/job pins')
 validate_artifact(meta,expected,selected['id'],selected['outer_sha256'])
 need(meta['size_in_bytes']==selected['bytes'] and meta['expires_at']==selected['expires_at'],'Original new upload metadata changed')
 need(run.get('id')==expected['run_id'] and type(run.get('run_attempt')) is int and run['run_attempt']==expected['run_attempt'] and run.get('head_sha')==expected['commit'] and run.get('path')==expected['workflow_path'] and run.get('repository',{}).get('full_name')==REPO and run.get('status')=='completed' and run.get('conclusion')=='success','Producer attempt not terminal success')
 need(positive(job.get('id')) and type(job.get('run_id')) is int and type(job.get('run_attempt')) is int and job.get('id')==selected['producer_job_id'] and job.get('run_id')==expected['run_id'] and job.get('run_attempt')==expected['run_attempt'] and job.get('head_sha')==expected['commit'] and (job.get('name')==JOB_NAME or job.get('name','').endswith(' / '+JOB_NAME)) and job.get('status')=='completed' and job.get('conclusion')=='success','Producer job not terminal success')
 need(receipt.get('assets')==ASSETS and receipt.get('historical_origin')==HISTORY and receipt.get('input_before')==receipt.get('input_after') and receipt.get('production_integration_allowed') is False,'Producer receipt byte/provenance scope changed')
 return True

def verify_new_delivery(archive,destination,receipt,run,job,meta,expected,upload_pin):
    validate_terminal_origin(receipt,run,job,meta,expected,upload_pin)
    need(file_record(archive)=={'bytes':meta['size_in_bytes'],'sha256':meta['digest'].removeprefix('sha256:')},'Whole new outer artifact hash/size differs')
    root=archives.extract_archive(Path(archive),Path(destination),require_single_root=False,max_entries=2,max_uncompressed_bytes=64*1024**2)
    need(set(archives.inventory(root))==set(ASSETS),'Exact two-asset delivery required')
    for name,row in ASSETS.items():need(file_record(root/name)==row,'Delivered resource/source differs from immutable original')
    return {'whole_new_outer_verified':True,'new_artifact_id':meta['id'],'assets':ASSETS,'production_integration_allowed':False}

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('operation',choices=['contract','prepare','capture']);parser.add_argument('--input',type=Path);parser.add_argument('--output',type=Path);parser.add_argument('--artifact-id',type=int);parser.add_argument('--artifact-digest');a=parser.parse_args();c=contract()
 if a.operation=='contract':
  if os.environ.get('GITHUB_OUTPUT'):
   with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('input_commit='+c['input_commit']+'\n')
  print(json.dumps({'input_commit':c['input_commit'],'input_tree':c['input_tree'],'contract':c['contract']}));return
 i=current_identity();need(a.input is not None and a.output is not None,'Explicit input and output paths required')
 temp=Path(os.environ.get('RUNNER_TEMP','')).resolve();need(temp.is_dir() and a.output.resolve()!=temp and a.output.resolve().is_relative_to(temp),'CLI output must be inside actual RUNNER_TEMP')
 need(git(ROOT,'rev-parse','HEAD').decode().strip()==i['commit'],'Driver checkout differs from actual workflow SHA')
 if a.operation=='prepare':result=prepare(a.input,a.output,c,i)
 else:
  result=capture_upload(a.output,c,a.input,i,a.artifact_id,a.artifact_digest)
  if os.environ.get('GITHUB_OUTPUT'):
   upload=result['new_artifact']
   with open(os.environ['GITHUB_OUTPUT'],'a') as f:f.write('artifact_bytes='+str(upload['bytes'])+'\nproducer_job_id='+str(upload['producer_job_id'])+'\n')
 print(json.dumps({'status':result['status'],'artifact_name':result['artifact_name'],'production_integration_allowed':False}))
if __name__=='__main__':main()
