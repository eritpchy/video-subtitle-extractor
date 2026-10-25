#!/usr/bin/env python3
"""Prepare pinned cross-platform native OCR assets; build-time tools only.

Downloads exact official Paddle archives, verifies every source and converted
byte, and exports deterministic JSON configs. No package installation is ever
performed. Output must be absent or empty; artifacts are committed atomically.
"""
from __future__ import annotations
import argparse,hashlib,importlib.metadata,json,os,re,shutil,subprocess,sys,tarfile,tempfile,time,urllib.error,urllib.request
from pathlib import Path,PurePosixPath

HERE=Path(__file__).resolve().parent
MAX_ARCHIVE_BYTES=512*1024*1024

def digest(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
 return h.hexdigest()
def canonical(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')
def load_pins(path):
 p=json.loads(Path(path).read_text(encoding='utf-8'))
 if p.get('schema_version')!=1 or len(p.get('models',[]))!=15:raise ValueError('Expected schema 1, exactly 15 pinned models')
 if p.get('converter')!={'name':'paddle2onnx','version':'2.1.0','opset':17,'auto_update_opset':False,'checker':True,'optimizer':'None','quantization':False,'autoinstall':False}:raise ValueError('Unsupported conversion contract')
 names=set()
 for m in p['models']:
  name=m['name']
  if not re.fullmatch(r'[A-Za-z0-9_-]+',name) or name in names:raise ValueError('Invalid/duplicate model name')
  names.add(name)
  expected='https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/'+name+'_infer.tar'
  if m['url']!=expected:raise ValueError('Unapproved official source URL')
  if set(m['files'])!={'inference.json','inference.pdiparams','inference.yml'}:raise ValueError('Unexpected source file set')
  for h in [m['archive_sha256'],m['onnx_sha256'],*m['files'].values()]:
   if not re.fullmatch('[0-9a-f]{64}',h):raise ValueError('Invalid pin')
 return p

def check_versions(pins):
 for package,expected in pins['tool_versions'].items():
  actual=importlib.metadata.version(package)
  if actual!=expected:raise RuntimeError(f'Tool version mismatch: {package} expected {expected}, got {actual}')

def conversion_environment(**overrides):
 # No converter path may invoke pip/automatic optimizer installation. -I below
 # additionally prevents PYTHONPATH/user-site from masking missing dependencies.
 env=dict(os.environ);env.update(overrides)
 env.update(POLYGRAPHY_AUTOINSTALL_DEPS='0',PIP_NO_INDEX='1',
            PYTHONNOUSERSITE='1',PYTHONHASHSEED='0',ORT_DISABLE_TELEMETRY='1')
 return env

def declared_build_versions(requirements=None):
 versions={}
 for raw in Path(requirements or HERE/'requirements-model-build.txt').read_text(encoding='utf-8').splitlines():
  line=raw.strip()
  if not line or line.startswith('#'):continue
  match=re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)',line)
  if not match:raise ValueError('Conversion dependencies must all be explicit exact pins: '+line)
  package,version=match.groups();key=package.lower().replace('_','-').replace('.','-')
  if key in versions:raise ValueError('Duplicate conversion dependency: '+package)
  versions[key]=version
 return versions

def preflight_tools(pins):
 if sys.version_info[:2]!=(3,12):raise RuntimeError('Deterministic conversion requires Python 3.12')
 declared=declared_build_versions()
 for package,expected in declared.items():
  try:actual=importlib.metadata.version(package)
  except importlib.metadata.PackageNotFoundError as error:
   raise RuntimeError(f'Missing conversion dependency {package}=={expected}; install requirements-model-build.txt in the isolated build environment') from error
  if actual!=expected:raise RuntimeError(f'Conversion dependency mismatch: {package} expected {expected}, got {actual}')
 for package,version in pins['tool_versions'].items():
  if declared.get(package.lower().replace('_','-').replace('.','-'))!=version:
   raise RuntimeError('Requirements conflict with model source tool pin: '+package)
 # Metadata checks alone missed the upstream undeclared packaging import. Run
 # the real command/C++ extension import under the exact isolated interpreter.
 code="""import importlib,json
modules=('packaging.version','numpy','yaml','google.protobuf','paddle','paddle2onnx.command','paddle2onnx.paddle2onnx_cpp2py_export','onnx','polygraphy')
for name in modules: importlib.import_module(name)
from paddle2onnx.command import arg_parser
assert arg_parser().parse_args(['--optimize_tool','None']).optimize_tool == 'None'
print(json.dumps({'imports':list(modules),'optimizer':'None','autoinstall':False}))
"""
 try:result=subprocess.run([sys.executable,'-I','-c',code],env=conversion_environment(),
                           stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=120)
 except subprocess.TimeoutExpired as error:raise RuntimeError('Conversion dependency preflight timed out before downloading models') from error
 if result.returncode:raise RuntimeError('Conversion dependency preflight failed before downloading models; no packages were auto-installed:\n'+result.stdout[-6000:])
 return declared

def get_archive(row,cache,offline):
 destination=cache/(row['archive_sha256']+'.tar')
 if destination.exists():
  if not destination.is_file() or destination.is_symlink() or digest(destination)!=row['archive_sha256']:raise ValueError('Corrupt cached source; refusing replacement')
  return destination
 if offline:raise FileNotFoundError(f"Offline source missing: {destination}")
 for attempt in range(3):
  temp=None
  try:
   with urllib.request.urlopen(row['url'],timeout=120) as response:
    if not response.geturl().startswith('https://paddle-model-ecology.bj.bcebos.com/'):raise ValueError('Unexpected redirect source')
    with tempfile.NamedTemporaryFile(dir=cache,prefix='.download-',delete=False) as f:
     temp=Path(f.name);size=0
     while part:=response.read(1024*1024):
      size+=len(part)
      if size>MAX_ARCHIVE_BYTES:raise ValueError('Archive exceeds bound')
      f.write(part)
   if temp.stat().st_size!=row['archive_bytes'] or digest(temp)!=row['archive_sha256']:raise ValueError('Downloaded archive hash/size mismatch')
   os.replace(temp,destination);return destination
  except urllib.error.HTTPError as e:
   if e.code not in (429,500,502,503,504) or attempt==2:raise
   time.sleep(attempt+1)
  except TimeoutError:
   if attempt==2:raise
   time.sleep(attempt+1)
  finally:
   if temp is not None and temp.exists():temp.unlink()
 raise RuntimeError('Source download exhausted')

def extract_sources(archive,row,destination):
 destination.mkdir()
 with tarfile.open(archive) as tf:
  found={}
  for member in tf.getmembers():
   path=PurePosixPath(member.name)
   if path.is_absolute() or '..' in path.parts or not(member.isfile() or member.isdir()):raise ValueError('Unsafe source archive member')
   if path.name not in row['files']:continue
   if not member.isfile() or path.name in found:raise ValueError('Duplicate/non-file inference member')
   # Only extract the three pinned inputs, never archive-provided paths or code.
   if member.size>MAX_ARCHIVE_BYTES:raise ValueError('Oversized source member')
   source=tf.extractfile(member)
   with (destination/path.name).open('wb') as f:shutil.copyfileobj(source,f)
   found[path.name]=digest(destination/path.name)
 if found!=row['files']:raise ValueError('Source file identity/hash mismatch')
 return destination

def prepare(args):
 pins=load_pins(args.pins);check_versions(pins);preflight_tools(pins)
 import yaml,onnx
 output=args.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
 if output.exists() and (not output.is_dir() or any(output.iterdir())):raise ValueError('Output must be absent or empty; refusing replacement')
 cache=args.cache.resolve();cache.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='.native-models-',dir=output.parent) as temporary:
  work=Path(temporary);stage=work/'stage';stage.mkdir();(stage/'models').mkdir();(stage/'config').mkdir();(work/'source').mkdir();(work/'tmp').mkdir();configs={};manifest=[]
  for row in pins['models']:
   archive=get_archive(row,cache,args.offline);source=extract_sources(archive,row,work/'source'/row['name']);cfg=yaml.safe_load((source/'inference.yml').read_text(encoding='utf-8'))
   if cfg.get('Global',{}).get('model_name')!=row['name']:raise ValueError('YAML model identity mismatch')
   configs[row['name']]=cfg;target=stage/'models'/(row['name']+'_infer.onnx')
   command=[sys.executable,'-I','-c','from paddle2onnx.command import main; main()','--model_dir',str(source),'--model_filename','inference.json','--params_filename','inference.pdiparams','--save_file',str(target),'--opset_version','17','--enable_auto_update_opset','False','--enable_onnx_checker','True','--optimize_tool','None']
   env=conversion_environment(TMPDIR=str(work/'tmp'))
   result=subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
   if result.returncode:raise RuntimeError(f"Conversion failed: {row['name']}\n{result.stdout[-6000:]}")
   if target.stat().st_size!=row['onnx_bytes'] or digest(target)!=row['onnx_sha256']:raise ValueError(f"Converted model differs from pinned no-optimizer output: {row['name']}")
   model=onnx.load(str(target));onnx.checker.check_model(model)
   if [(x.domain,x.version) for x in model.opset_import]!=[('',17)]:raise ValueError('Unexpected ONNX opset/domain')
   manifest.append({'name':row['name']+'_infer','rec_model':row['name'] if row['name'].endswith('_rec') else None,'onnx_sha256':row['onnx_sha256'],'onnx_bytes':row['onnx_bytes'],'source_url':row['url'],'source_archive_sha256':row['archive_sha256'],'source':{k:{'sha256':v} for k,v in row['files'].items()},'languages':row.get('languages',[]),'converter':pins['converter']})
   print('verified '+row['name'],flush=True)
  for tier in ('small','medium'):
   data=canonical({kind:configs[f'PP-OCRv6_{tier}_{kind}'] for kind in ('det','rec')});(stage/'config'/f'{tier}.json').write_bytes(data)
  for row in pins['models']:
   if not row['name'].startswith('PP-OCRv6_'):(stage/'config'/(row['name']+'.json')).write_bytes(canonical(configs[row['name']]))
  for row in manifest:
   name=row['name'].removesuffix('_infer');config=f"config/{name.split('_')[1]}.json" if name.startswith('PP-OCRv6_') else f'config/{name}.json';row['config_file']=config;row['config_sha256']=digest(stage/config)
   if row['config_sha256']!=pins['config_sha256'][config]:raise ValueError('Config differs from pinned deterministic export')
  (stage/'model-manifest.json').write_bytes(canonical(manifest));(stage/'model-source-pins.json').write_bytes(canonical(pins));(stage/'unavailable-models.json').write_bytes(b'[]\n')
  if output.exists():output.rmdir()
  os.replace(stage,output)
 return output

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--pins',type=Path,default=HERE/'model-source-pins.json');parser.add_argument('--output',type=Path,required=True);parser.add_argument('--cache',type=Path,required=True);parser.add_argument('--offline',action='store_true');args=parser.parse_args();prepare(args)
if __name__=='__main__':main()
