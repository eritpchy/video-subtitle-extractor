#!/usr/bin/env python3
"""Fetch pinned official ORT CPU SDK without shipping Python/debug symbols.

Standard-library build helper. Manifest must be reviewed/version-controlled beside
this file. No latest-version discovery and no installation into the host OS.
"""
from __future__ import annotations
import argparse,hashlib,json,os,pathlib,shutil,tarfile,tempfile,urllib.request,zipfile

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def safe_relative(value):
    p=pathlib.PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or '\\' in value or ':' in value:
        raise ValueError('unsafe manifest path: '+value)
    return p

def verify(root,record,runtime_only=False):
    chosen=[f for f in record['files'] if not runtime_only or f['role'] in ('runtime','notice')]
    for f in chosen:
        p=root/pathlib.Path(safe_relative(f['path']))
        if p.is_symlink() or not p.is_file() or p.stat().st_size!=f['bytes'] or sha256(p)!=f['sha256']:
            raise ValueError('SDK file integrity mismatch: '+str(p))
    for name,target in record['symlinks'].items():
        p=root/pathlib.Path(safe_relative(name))
        if not p.is_symlink() or os.readlink(p)!=target:raise ValueError('SDK symlink mismatch: '+str(p))
    return chosen

def fetch(target,output,cache,manifest=None,runtime_only=False,offline=False):
    manifest=pathlib.Path(manifest or pathlib.Path(__file__).with_name('ort-sdk-manifest.json'))
    doc=json.loads(manifest.read_text(encoding='utf-8'));rec=doc['targets'][target]
    safe_relative(rec['archive_root'])
    for f in rec['files']:safe_relative(f['path'])
    for name,target_name in rec['symlinks'].items():
        safe_relative(name);safe_relative(target_name)
        if '/' in target_name:raise ValueError('symlink target must be basename')
    output=pathlib.Path(output).absolute();cache=pathlib.Path(cache).absolute()
    if output.is_symlink():raise ValueError('output must not be a symlink')
    if output.exists():
        verify(output,rec,runtime_only);return output
    cache.mkdir(parents=True,exist_ok=True);a=rec['archive'];archive=cache/a['filename']
    if pathlib.Path(a['filename']).name!=a['filename']:raise ValueError('invalid archive filename')
    if archive.is_symlink():raise ValueError('cache file must not be a symlink')
    if not archive.exists():
        if offline:raise FileNotFoundError('offline mode: missing verified archive '+str(archive))
        fd,temp=tempfile.mkstemp(prefix='.ort-download-',dir=cache);os.close(fd)
        try:
            with urllib.request.urlopen(a['url'],timeout=120) as src,open(temp,'wb') as dst:
                shutil.copyfileobj(src,dst,length=1024*1024)
            if os.path.getsize(temp)!=a['bytes'] or sha256(temp)!=a['sha256']:raise ValueError('archive SHA256/size mismatch')
            os.replace(temp,archive)
        finally:
            if os.path.exists(temp):os.unlink(temp)
    if archive.stat().st_size!=a['bytes'] or sha256(archive)!=a['sha256']:raise ValueError('cached archive SHA256/size mismatch')
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=pathlib.Path(tempfile.mkdtemp(prefix='.ort-stage-',dir=output.parent))
    try:
        chosen=[f for f in rec['files'] if not runtime_only or f['role'] in ('runtime','notice')]
        def write_file(f,stream):
            dest=stage/pathlib.Path(safe_relative(f['path']));dest.parent.mkdir(parents=True,exist_ok=True)
            with open(dest,'wb') as dst:shutil.copyfileobj(stream,dst,length=1024*1024)
            if dest.stat().st_size!=f['bytes'] or sha256(dest)!=f['sha256']:raise ValueError('extracted SHA256 mismatch: '+f['path'])
            if f['role']=='runtime' and os.name!='nt':dest.chmod(0o755)
        if a['filename'].endswith('.zip'):
            with zipfile.ZipFile(archive) as z:
                for f in chosen:
                    member=rec['archive_root']+'/'+str(safe_relative(f['path']))
                    with z.open(member) as stream:write_file(f,stream)
        else:
            with tarfile.open(archive,'r:gz') as t:
                members={m.name.removeprefix('./'):m for m in t.getmembers()}
                for f in chosen:
                    member=members[rec['archive_root']+'/'+str(safe_relative(f['path']))]
                    if not member.isfile():raise ValueError('selected tar member is not a regular file')
                    with t.extractfile(member) as stream:write_file(f,stream)
        for name,target_name in rec['symlinks'].items():
            path=stage/pathlib.Path(safe_relative(name));safe_relative(target_name)
            if '/' in target_name:raise ValueError('symlink target must be basename')
            path.symlink_to(target_name)
        verify(stage,rec,runtime_only)
        (stage/'sdk-provenance.json').write_text(json.dumps(dict(version=doc['version'],target=target,archive=a,git_commit=rec['git_commit'],runtime_only=runtime_only),indent=2),encoding='utf-8')
        stage.rename(output)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True,choices=['linux-x64','windows-x64','macos-arm64'])
    p.add_argument('--output',required=True,type=pathlib.Path);p.add_argument('--cache',required=True,type=pathlib.Path)
    p.add_argument('--manifest',type=pathlib.Path);p.add_argument('--runtime-only',action='store_true');p.add_argument('--offline',action='store_true',help='never download; require verified existing output or cached archive')
    a=p.parse_args();print(fetch(a.target,a.output,a.cache,a.manifest,a.runtime_only,a.offline))
