#!/usr/bin/env python3
"""Pinned app-local VC runtime, no installer execution or system DLL copying.

License eligibility is a distributor obligation; this helper verifies provenance,
not entitlement. Only supports the exact reviewed, SHA256-pinned MSZIP cabinets.
"""
import argparse,hashlib,json,os,pathlib,shutil,struct,tempfile,urllib.request,urllib.parse,zlib,subprocess

DLL_NAMES={'msvcp140.dll','msvcp140_1.dll','vcruntime140.dll','vcruntime140_1.dll','concrt140.dll'}
NOTICE_FILE='Microsoft-Visual-Cpp-runtime-NOTICE.txt'
LICENSE_NAMES={NOTICE_FILE}  # Compatibility name: project notice, not Microsoft license text.

def digest(b):return hashlib.sha256(b).hexdigest()
def checked(b,rec):
    if len(b)!=rec['bytes'] or digest(b)!=rec['sha256']:raise ValueError('VC source integrity mismatch')
    return b

def cab_files(data,wanted):
    """Bounded reader for pinned single-folder MSZIP CABs; never writes paths."""
    if len(data)<44 or data[:4]!=b'MSCF' or struct.unpack_from('<I',data,8)[0]>len(data):raise ValueError('invalid CAB')
    nfolders,nfiles,flags=struct.unpack_from('<HHH',data,26)
    if nfolders!=1 or nfiles>128 or flags&~4:raise ValueError('unsupported CAB layout')
    header=36;folder_extra=block_extra=0
    if flags&4:
        reserve,folder_extra,block_extra=struct.unpack_from('<HBB',data,header);header+=4+reserve
    start,blocks,method=struct.unpack_from('<IHH',data,header)
    if method!=1 or blocks>2048:raise ValueError('unsupported CAB compression/size')
    entries={};off=struct.unpack_from('<I',data,16)[0]
    for _ in range(nfiles):
        size,offset,folder=struct.unpack_from('<IIH',data,off);end=data.index(b'\0',off+16)
        name=data[off+16:end].decode('ascii');off=end+1
        if folder!=0 or name in entries:raise ValueError('unsupported CAB file')
        entries[name]=(offset,size)
    out=bytearray();off=start
    for _ in range(blocks):
        _,compressed,plain=struct.unpack_from('<IHH',data,off);off+=8+block_extra
        payload=data[off:off+compressed];off+=compressed
        if len(payload)!=compressed or payload[:2]!=b'CK' or plain>32768:raise ValueError('invalid MSZIP block')
        decomp=zlib.decompressobj(-15,zdict=bytes(out[-32768:])) if out else zlib.decompressobj(-15)
        chunk=decomp.decompress(payload[2:],plain+1)
        if len(chunk)!=plain or not decomp.eof or decomp.unconsumed_tail:raise ValueError('invalid decompression bounds')
        out.extend(chunk)
        if len(out)>64*1024*1024:raise ValueError('oversize CAB output')
    result={}
    for name in wanted:
        offset,size=entries[name]
        if offset+size>len(out):raise ValueError('invalid CAB file bounds')
        result[name]=bytes(out[offset:offset+size])
    return result

def extract_cab(data,wanted):
    if os.name!='nt':return cab_files(data,wanted)
    # Native Windows CAB expansion, never execute the redist installer.
    expand=pathlib.Path(os.environ['SystemRoot'])/'System32'/'expand.exe'
    if not expand.is_file():raise RuntimeError('Windows expand.exe not found')
    with tempfile.TemporaryDirectory(prefix='vse-vc-cab-') as temp:
        root=pathlib.Path(temp);cab=root/'source.cab';cab.write_bytes(data);out=root/'out';out.mkdir()
        for name in wanted:
            if pathlib.PurePath(name).name!=name or '/' in name or '\\' in name:raise ValueError('invalid CAB member')
            subprocess.run([str(expand),'-F:'+name,str(cab),str(out)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        return {name:(out/name).read_bytes() for name in wanted}

def load_manifest(path=None):
    m=json.loads(pathlib.Path(path or pathlib.Path(__file__).with_name('vc-runtime-manifest.json')).read_text())
    expected=DLL_NAMES|LICENSE_NAMES
    if {f['name'] for f in m['files']}!=expected or len(m['files'])!=len(expected):raise ValueError('unexpected VC allowlist')
    if m.get('schema')!=2 or m.get('license_file')!=NOTICE_FILE:raise ValueError('unexpected VC notice contract')
    if m.get('licensing_review',{}).get('status')!='applicable-terms-not-independently-verified' or m['licensing_review'].get('final_publication_authorized') is not False:raise ValueError('Unreviewed licensing status')
    for f in m['files']:
        if f['name'] in DLL_NAMES:
            if f.get('cab_member')!=f['name']+'_amd64' or 'url' in f:raise ValueError('unsafe CAB member')
            if f.get('file_version')!=m['version']:raise ValueError('inconsistent VC runtime version')
        else:
            if 'cab_member' in f or 'url' in f or f.get('source_file')!='vc-runtime-NOTICE.txt':
                raise ValueError('unexpected local VC attribution source')
    url=urllib.parse.urlsplit(m['installer']['url'])
    if url.scheme!='https' or url.hostname!='download.visualstudio.microsoft.com' or not url.path.endswith('/VC_redist.x64.exe'):
        raise ValueError('Unexpected runtime installer source')
    return m
def verify_source(runtime_dir,license_path,manifest=None):
    """Validate exact DLLs and the separately pinned project attribution notice."""
    m=load_manifest(manifest);root=pathlib.Path(runtime_dir);lic=pathlib.Path(license_path)
    if root.is_symlink():raise ValueError('linked runtime directory rejected')
    values={}
    for rec in m['files']:
        p=lic if rec['name']==m['license_file'] else root/rec['name']
        if p.is_symlink() or not p.is_file():raise ValueError('missing/linked VC source: '+str(p))
        b=checked(p.read_bytes(),rec)
        if rec['name'].endswith('.dll'):
            if b[:2]!=b'MZ':raise ValueError('not a PE DLL')
            pe=struct.unpack_from('<I',b,0x3c)[0]
            if b[pe:pe+4]!=b'PE\0\0' or struct.unpack_from('<H',b,pe+4)[0]!=0x8664:raise ValueError('not x64 PE')
        values[rec['name']]=b
    return m,values

def local_notice(rec):
    path=pathlib.Path(__file__).with_name(rec['source_file'])
    if path.is_symlink() or not path.is_file():raise ValueError('Missing/linked local runtime attribution')
    return checked(path.read_bytes(),rec)

def cached_source(rec,cache,name,offline=False):
    """Content-addressed cache: a reviewed pin update never reuses an older EXE."""
    if len(rec['sha256'])!=64 or any(c not in '0123456789abcdef' for c in rec['sha256']):raise ValueError('invalid VC source digest')
    if name!='VC_redist.x64.exe':raise ValueError('Only the pinned runtime installer may be downloaded')
    url=urllib.parse.urlsplit(rec['url'])
    if url.scheme!='https' or url.hostname!='download.visualstudio.microsoft.com' or not url.path.endswith('/VC_redist.x64.exe'):
        raise ValueError('Only the official runtime installer URL may be downloaded')
    if pathlib.Path(cache).is_symlink():raise ValueError('linked cache rejected')
    archive=pathlib.Path(cache)/rec['sha256']/name
    if archive.parent.is_symlink() or archive.is_symlink():raise ValueError('linked archive rejected')
    if not archive.exists():
        if offline:raise FileNotFoundError('offline: missing '+str(archive))
        with urllib.request.urlopen(rec['url'],timeout=120) as response:b=checked(response.read(rec['bytes']+1),rec)
        archive.parent.mkdir(parents=True,exist_ok=True)
        archive.write_bytes(b)
    return checked(archive.read_bytes(),rec)

def fetch(output,cache,manifest=None,offline=False,acknowledge_license=False):
    if not acknowledge_license:raise PermissionError('Distributor must explicitly confirm applicable Microsoft redistribution license rights')
    m=load_manifest(manifest);output=pathlib.Path(output);cache=pathlib.Path(cache)
    notices={f['name']:local_notice(f) for f in m['files'] if f['name'] in LICENSE_NAMES}
    cache.mkdir(parents=True,exist_ok=True)
    if output.exists():verify_source(output,output/m['license_file'],manifest);return output
    data=cached_source(m['installer'],cache,'VC_redist.x64.exe',offline);parts={}
    for cab in m['cabinets']:
        blob=data[cab['offset']:cab['offset']+cab['bytes']];checked(blob,cab)
        parts.update(extract_cab(blob,cab['members']))
    inner=checked(parts[m['runtime_cab']['member']],m['runtime_cab'])
    dlls=extract_cab(inner,[f['cab_member'] for f in m['files'] if f['name'].endswith('.dll')])
    # This local project notice is independent of Microsoft license documents.
    # No historical DOCX is fetched/read, including from an existing cache.
    output.parent.mkdir(parents=True,exist_ok=True);stage=pathlib.Path(tempfile.mkdtemp(prefix='.vc-runtime-',dir=output.parent))
    try:
        for f in m['files']:
            b=notices[f['name']] if f['name'] in LICENSE_NAMES else dlls[f['cab_member']]
            (stage/f['name']).write_bytes(checked(b,f))
        verify_source(stage,stage/m['license_file'],manifest)
        (stage/'vc-runtime-provenance.json').write_text(json.dumps(m,indent=2));stage.rename(output)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return output

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--cache',required=True);p.add_argument('--manifest');p.add_argument('--offline',action='store_true');p.add_argument('--acknowledge-redistribution-license',action='store_true',help='Distributor asserts applicable Microsoft license permits this redistribution');a=p.parse_args();print(fetch(a.output,a.cache,a.manifest,a.offline,a.acknowledge_redistribution_license))
