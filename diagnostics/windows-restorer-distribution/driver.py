#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Explicit final-input distribution driver: local preparation, never publication."""
from __future__ import annotations
import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import stat

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('_vse_source_only_driver', HERE / 'source_only.py')
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
PINS_PATH = HERE / 'pins.json'
REPO = 'eritpchy/video-subtitle-extractor'


def load_pins():
    # The checked-in JSON and both no-checkout bootstraps share one exact anchor.
    raw = s.plain(PINS_PATH).read_bytes()
    s.need(raw == s.configured_pin_bytes(), 'Checked-in/embedded input pins differ')
    return s.expected_pins()


def verify_origin(pin, profile, token):
    s.validate_input_pins(pin)
    row = pin['profiles'][profile]
    run = api(f'/repos/{REPO}/actions/runs/{pin["run_id"]}/attempts/{pin["run_attempt"]}', token)
    job = api(f'/repos/{REPO}/actions/jobs/{row["job"]}', token)
    candidate = api(f'/repos/{REPO}/actions/jobs/{pin["candidate_set_job"]}', token)
    s.need(type(run.get('id')) is int and type(run.get('run_attempt')) is int
           and run.get('id') == pin['run_id'] and run.get('head_sha') == pin['commit']
           and run.get('run_attempt') == pin['run_attempt'] and run.get('status') == 'completed'
           and run.get('conclusion') == 'success' and run.get('path') == '.github/workflows/release.yml'
           and run.get('head_branch') == 'fix/restore-release-matrix'
           and run.get('repository', {}).get('full_name') == REPO, 'Final main run/attempt/source did not pass')
    for value, name in ((job, profile + ' / windows-2022 (GPU hardware not tested)'),
                        (candidate, 'Verify approved candidate matrix (not publication)')):
        s.need(type(value.get('id')) is int and type(value.get('run_id')) is int and type(value.get('run_attempt')) is int
               and value.get('run_id') == pin['run_id'] and value.get('run_attempt') == pin['run_attempt']
               and value.get('head_sha') == pin['commit'] and value.get('status') == 'completed'
               and value.get('conclusion') == 'success' and value.get('name') == name,
               'Final canonical producer/candidate-set job did not pass')
    s.need(job.get('id') == row['job'] and candidate.get('id') == pin['candidate_set_job'], 'Wrong original job identity')
    return {'run':run, 'job':job, 'candidate_set':candidate}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def api(path, token):
    need_path = path.startswith('/repos/' + REPO + '/')
    s.need(need_path, 'Untrusted API path')
    request = urllib.request.Request('https://api.github.com' + path, headers={
        'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token,
        'User-Agent': 'vse-restorer-distribution-evidence'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as reply:
        data = reply.read(4 * 1024 ** 2 + 1)
        s.need(len(data) <= 4 * 1024 ** 2, 'Oversized API response')
        return s.json_bytes(data)


def artifact_metadata(artifact_id, token, *, name, run_id, commit, expected=None):
    s.need(type(artifact_id) is int and artifact_id > 0, 'Artifact ID required')
    row = api(f'/repos/{REPO}/actions/artifacts/{artifact_id}', token)
    s.need(row.get('id') == artifact_id and row.get('name') == name and row.get('expired') is False
           and row.get('workflow_run', {}).get('id') == run_id
           and row.get('workflow_run', {}).get('head_sha') == commit, 'Artifact identity/run/source mismatch')
    s.need(type(row.get('size_in_bytes')) is int and 0 < row['size_in_bytes'] < 16 * 1024 ** 3, 'Artifact size bound')
    s.need(isinstance(row.get('digest'), str) and row['digest'].startswith('sha256:'), 'Artifact digest missing')
    s.hash_text(row['digest'][7:])
    expiry = datetime.datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
    s.need(expiry > datetime.datetime.now(datetime.timezone.utc), 'Artifact expired')
    if expected is not None:
        s.need(row['size_in_bytes'] == expected['bytes'] and row['digest'][7:] == expected['sha256']
               and row['expires_at'] == expected['expiry'], 'Canonical artifact metadata differs from fixed pins')
    return row


def download_artifact(metadata, destination, token):
    request = urllib.request.Request(f'https://api.github.com/repos/{REPO}/actions/artifacts/{metadata["id"]}/zip',
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'vse-restorer-distribution-evidence'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            s.need(response.status == 302, 'Artifact redirect required')
    except urllib.error.HTTPError as error:
        s.need(error.code == 302, 'Artifact did not provide its authenticated signed redirect')
        location = error.headers.get('Location', '')
    else: raise RuntimeError('Artifact redirect required')
    url = urllib.parse.urlsplit(location)
    s.need(url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.fragment,
           'Unsafe signed artifact location')
    count = 0
    # No Authorization forwarded to storage; signed URL/query never logged.
    with urllib.request.urlopen(location, timeout=120) as reply, destination.open('xb') as output:
        while chunk := reply.read(1024 ** 2):
            count += len(chunk); s.need(count <= metadata['size_in_bytes'], 'Artifact exceeded fixed bytes'); output.write(chunk)
        output.flush(); os.fsync(output.fileno())
    s.need(s.file_record(destination) == {'bytes': metadata['size_in_bytes'], 'sha256': metadata['digest'][7:]}, 'Whole outer artifact SHA mismatch')
    return {'metadata': metadata, 'whole_outer_sha256_verified': True, 'outer': s.file_record(destination)}


def checked_source(root):
    pin = load_pins()
    root = root.resolve(strict=True)
    sha = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'], text=True).strip()
    status = subprocess.check_output(['git','-C',str(root),'status','--porcelain','--untracked-files=all'], text=True)
    s.need(sha == pin['commit'] and not status, 'Exact clean final application/build checkout required')
    return root


def source_git_bytes(root, *args):
    """Read local Git objects without replacements, filters, hooks or lazy fetch."""
    env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS='1',
               GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    return subprocess.check_output(['git','--no-replace-objects','-c','core.fsmonitor=false',
        '-c','protocol.allow=never','-C',str(root),*args], env=env, stderr=subprocess.PIPE)


def source_closure_paths(raw):
    tree = ast.parse(raw)
    values = [node.value for node in tree.body if isinstance(node,ast.Assign)
              and any(isinstance(target,ast.Name) and target.id=='SOURCE_PATHS' for target in node.targets)]
    s.need(len(values)==1, 'One authenticated source closure required')
    def literal(node, names=None):
        names = names or {}
        if isinstance(node,ast.Constant) and isinstance(node.value,str): return node.value
        if isinstance(node,ast.Tuple): return tuple(literal(item,names) for item in node.elts)
        if isinstance(node,ast.Name) and node.id in names: return names[node.id]
        if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add): return literal(node.left,names)+literal(node.right,names)
        if (isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tuple'
            and len(node.args)==1 and not node.keywords and isinstance(node.args[0],ast.GeneratorExp)):
            expr=node.args[0]; s.need(len(expr.generators)==1,'One literal closure generator required')
            loop=expr.generators[0]
            s.need(isinstance(loop.target,ast.Name) and not loop.ifs and not loop.is_async,'Unsafe closure generator')
            return tuple(literal(expr.elt,{**names,loop.target.id:value}) for value in literal(loop.iter,names))
        raise RuntimeError('Nonliteral authenticated source closure')
    paths=literal(values[0])
    s.need(isinstance(paths,tuple) and len(paths)==len(set(paths))==55,'Exact 55-file source closure required')
    for path in paths:
        s.need(isinstance(path,str) and re.fullmatch(r'[A-Za-z0-9_./-]+',path)
               and not path.startswith('/') and not any(part in ('','.','..') for part in path.split('/')),
               'Unsafe authenticated source path')
    return paths


def authenticated_source_snapshot(root):
    pin=load_pins(); root=checked_source(root); objects={}; trees={}
    def obj(kind, identifier):
        s.need(re.fullmatch('[0-9a-f]{40}',identifier) is not None,'Invalid Git object identity')
        key=(kind,identifier)
        if key not in objects:
            raw=source_git_bytes(root,'cat-file',kind,identifier)
            s.need(len(raw)<=8*1024**2,'Oversize source Git object')
            actual=hashlib.sha1(kind.encode()+b' '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
            s.need(actual==identifier,'Source Git object bytes do not match authenticated ID')
            objects[key]=raw
        return objects[key]
    commit=obj('commit',pin['commit']); first=commit.split(b'\n',1)[0]
    s.need(re.fullmatch(rb'tree [0-9a-f]{40}',first) is not None,'Authenticated commit tree missing')
    tree_id=first[5:].decode()
    def entries(identifier):
        if identifier not in trees:
            raw=obj('tree',identifier); rows={}; position=0
            while position<len(raw):
                space=raw.index(b' ',position); end=raw.index(b'\0',space+1)
                mode=raw[position:space].decode('ascii'); name=raw[space+1:end].decode('utf-8')
                s.need(name not in rows and name not in ('','.','..') and '/' not in name and end+21<=len(raw),
                       'Invalid authenticated Git tree')
                rows[name]=(mode,raw[end+1:end+21].hex()); position=end+21
            trees[identifier]=rows
        return trees[identifier]
    def blob(path):
        parent=tree_id; parts=path.split('/')
        for index,name in enumerate(parts):
            rows=entries(parent); s.need(name in rows,'Missing authenticated source member')
            mode,identifier=rows[name]
            if index<len(parts)-1:
                s.need(mode=='40000','Non-tree source ancestor'); parent=identifier
            else:
                s.need(mode in ('100644','100755'),'Nonregular source Git blob')
                return obj('blob',identifier),identifier
    paths=source_closure_paths(blob('native/windows-restorer/source_bundle.py')[0])
    result={path:blob(path) for path in paths}
    return root,tree_id,result,len(objects)


def canonicalize_source_checkout(root):
    """Read-only pre-workload gate: checkout must already contain literal Git bytes."""
    root,tree,snapshot,count=authenticated_source_snapshot(root); rows=[]
    for name,(expected,identifier) in snapshot.items():
        path=root/name; s.plain(path)
        for parent in path.parents:
            if parent==root: break
            s.plain(parent,True)
        actual=path.read_bytes()
        s.need(actual==expected,'Source checkout differs from authenticated Git blob: '+name)
        rows.append({'path':name,'git_blob':identifier,'after':{'bytes':len(expected),'sha256':s.digest(expected)}})
    return {'scope':'pre-workload-canonical-Git-source-bytes','commit':load_pins()['commit'],'tree':tree,
            'files':rows,'file_count':len(rows),'raw_Git_objects_rehashed':count,
            'source_files_written':False,'global_or_persistent_Git_config_changed':False,
            'original_application_archives_or_parts_changed':False}


def verify_canonical_source_audit(root, audit):
    root,tree,snapshot,count=authenticated_source_snapshot(root)
    rows=audit.get('files')
    s.need(audit.get('commit')==load_pins()['commit'] and audit.get('tree')==tree
           and audit.get('file_count')==55 and isinstance(rows,list) and len(rows)==55
           and {row.get('path') for row in rows}==set(snapshot), 'Incomplete canonical source audit')
    for row in rows:
        expected,identifier=snapshot[row['path']]
        exact={'bytes':len(expected),'sha256':s.digest(expected)}
        s.need(row.get('git_blob')==identifier and row.get('after')==exact
               and s.file_record(root/row['path'])==exact, 'Canonical source audit or checkout differs from Git blob')
    return {'commit':load_pins()['commit'],'tree':tree,'file_count':55,'raw_Git_objects_rehashed':count}


def verify_source_zip_git_membership(root, archive):
    _,tree,snapshot,count=authenticated_source_snapshot(root); rows=[]
    with zipfile.ZipFile(s.plain(archive)) as stream:
        for name,(expected,identifier) in snapshot.items():
            actual=stream.read(name)
            s.need(actual==expected,'Delivered source ZIP differs from authenticated Git blob: '+name)
            rows.append({'path':name,'git_blob':identifier,'bytes':len(actual),'sha256':s.digest(actual)})
    return {'scope':'literal-source-ZIP-to-authenticated-Git-tree','commit':load_pins()['commit'],'tree':tree,
            'file_count':len(rows),'raw_Git_objects_rehashed':count,'files':rows,'source_zip':s.file_record(archive)}


def gate_at(source): return s.load_exact(source / 'native/windows-restorer/release_gate.py', 'gate')


def verify_rebuilt_map(source, path, expected):
    helper = s.load_exact(source/'native/windows-restorer/zlib_build.py', 'zlib_build')
    actual = helper.inspect_link_map(path)
    s.need(actual == expected, 'Raw rebuilt link-map policy differs')
    return actual


def output_values(values):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            for name, value in values.items():
                text = value if isinstance(value, str) else json.dumps(value, separators=(',', ':'))
                s.need('\n' not in text and '\r' not in text, 'Unsafe action output')
                stream.write(name + '=' + text + '\n')


def files(root):
    result = {}
    for path in root.rglob('*'):
        if path.is_dir(): s.plain(path, True)
        else: result[path.relative_to(root).as_posix()] = s.file_record(path)
    return result


def delivery_files(root):
    result = {'handoff.json':s.file_record(root/'handoff.json'),
              'producer-context.json':s.file_record(root/'producer-context.json')}
    for relative in ('volumes','build/distribution'):
        result.update({relative+'/'+name:row for name,row in files(root/relative).items()})
    return result


def checked_ancestors(path):
    path = Path(path).absolute()
    for ancestor in (path, *path.parents): s.plain(ancestor, True)
    return path


def owner_identity():
    result = {key:os.environ.get(key,'') for key in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_JOB')}
    s.need(all(result.values()), 'Actual job ownership required')
    s.need(result['GITHUB_RUN_ID'].isdigit() and result['GITHUB_RUN_ATTEMPT'].isdigit(), 'Invalid job identity')
    result['driver_sha256'] = s.sha(HERE/'driver.py')
    return result


def private_location(path):
    runner = checked_ancestors(os.environ['RUNNER_TEMP'])
    path = Path(path).absolute()
    s.need('..' not in path.parts, 'Noncanonical work path')
    s.need(path != runner and runner in path.parents, 'Work must be strictly inside actual RUNNER_TEMP')
    checked_ancestors(path.parent)
    return path


def fresh_work(path):
    path = private_location(path); path.mkdir(exist_ok=False)
    s.write_json(path/'job-owner.json', {'scope':'fresh-private-job-work','path':str(path), **owner_identity()})
    return path


def check_owned_work(path):
    path = private_location(path); checked_ancestors(path)
    marker = path/'job-owner.json'; s.plain(marker)
    s.need(marker.stat().st_nlink == 1, 'Linked ownership marker')
    s.need(s.read_json(marker) == {'scope':'fresh-private-job-work','path':str(path), **owner_identity()}, 'Wrong job/driver ownership')
    return path


def context_from_distribution(distribution, handoff, profile):
    build = s.read_json(distribution / 'build-record.json'); s.validate_compile(build, expected_exe=handoff['exe_sha256'])
    archive = distribution / 'restorer-source.zip'
    with zipfile.ZipFile(archive) as zipped:
        source_manifest = zipped.read('source-manifest.json')
        original_build = zipped.read('evidence/original-build-record.json')
    pin = load_pins()
    context = {'schema': 1, 'source_commit': pin['commit'], 'source_version': pin['version'],
        'application_identity':s.binding_identity(pin, profile), 'profile': profile,
        'source_zip_bytes': archive.stat().st_size, 'source_zip_sha256': s.sha(archive),
        'original_exe_sha256': handoff['exe_sha256'], 'handoff_sha256': s.sha(distribution.parent.parent / 'handoff.json'),
        'embedded_header_sha256': build['embedded_header_sha256'],
        'generator_identity_sha256': s.digest(s.canonical(build['generator_dependencies'])),
        'source_closure_sha256': s.digest(s.canonical(build['sources'])),
        'source_manifest_sha256': s.digest(source_manifest), 'original_build_record_sha256': s.digest(original_build)}
    s.validate_context(context)
    return context



def canonical_build_environment(record):
    env=s.initialize_msvc(record)
    # The allowlisted toolchain environment discards parent Git configuration.
    # Reintroduce only this reviewed command-scoped checkout conversion setting.
    env.update(GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='core.autocrlf',GIT_CONFIG_VALUE_0='false')
    record['build_git_command_configuration']={'core.autocrlf':False,'persistent_config_written':False}
    return env


def producer(args):
    s.need(args.acknowledge_license, 'Explicit distributor rights acknowledgment required')
    pin = load_pins()  # Incomplete future bindings fail before platform/I/O/network.
    s.native_server('windows-2022'); source = checked_source(args.source)
    notice = s.verify_project_notice(source, pin)
    work = fresh_work(args.work)
    row = pin['profiles'][args.profile]; token = os.environ.pop('GITHUB_TOKEN', '')
    s.need(token, 'Read-only artifact token required')
    record = {'schema': 1, 'scope': 'one-production-build-not-release', 'result': 'failed', 'profile': args.profile}
    failure = None
    try:
        checkout_audit = s.read_json(source.parent/'source-checkout-git-audit.json')
        record['canonical_source_pre_workload'] = verify_canonical_source_audit(source,checkout_audit)
        s.write_json(work/'source-checkout-git-audit.json',checkout_audit)
        origin = verify_origin(pin, args.profile, token)
        metadata = artifact_metadata(row['canonical_artifact_id'], token, name=row['canonical_artifact_name'], run_id=pin['run_id'],
            commit=pin['commit'], expected={'bytes':row['outer_container_bytes'],'sha256':row['outer_api_sha256'],'expiry':row['expiry_utc']})
        record['source_checkout_bytes'] = sum(p.stat().st_size for p in source.rglob('*') if p.is_file() and '.git' not in p.parts)
        record['budget_before_original_download'] = s.space(work, metadata['size_in_bytes'] + 2 * row['inner_zip_bytes'] + s.RESERVE)
        outer = work / 'original-canonical-outer.zip'
        record['original_download'] = download_artifact(metadata, outer, token)
        s.write_json(work / 'original-api-and-download.json', {**origin,'input_binding_sha256':s.APPLICATION_PINS_SHA256,'download':record['original_download']})
        original = work / 'original'
        expected = {row['inner_zip']: {'bytes':row['inner_zip_bytes'],'sha256':row['inner_zip_sha256']},
            row['inner_zip']+'.sha256': {'bytes':row['checksum_bytes'],'sha256':row['checksum_sha256']},
            row['inner_zip']+'.acceptance.json': {'bytes':row['receipt_bytes'],'sha256':row['receipt_sha256']}}
        s.unpack(outer, original, limit=row['inner_zip_bytes'] + 2 * 1024 ** 2, expected=expected)
        record['original_members'] = expected
        record['project_notice'] = notice
        env = canonical_build_environment(record)
        command = [sys.executable,'-B',str(source/'native/windows-restorer/release_gate.py'),'prepare',
            '--archive',str(original/row['inner_zip']),'--archive-sha256',row['inner_zip_sha256'],
            '--receipt-sha256',row['receipt_sha256'],'--checksum-sha256',row['checksum_sha256'],
            '--commit',pin['commit'],'--version',pin['version'],'--profile',args.profile,'--runner','windows-2022',
            '--work',str(work/'prepared'),'--acknowledge-redistribution-license']
        record['prepare_command'] = command
        with (work/'prepare.log').open('xb') as log:
            subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=3600, check=True)
        prepared = work/'prepared'; handoff = s.read_json(prepared/'handoff.json'); distribution = prepared/'build/distribution'
        context = context_from_distribution(distribution, handoff, args.profile)
        record['source_zip_git_membership'] = verify_source_zip_git_membership(source,distribution/'restorer-source.zip')
        gate = gate_at(source); manifest = prepared/'volumes'/(row['inner_zip']+'.transport.json')
        _, document = gate.load_handoff(prepared/'handoff.json',context['handoff_sha256'],distribution,manifest)
        s.need(document['archive']['uncompressed_bytes'] == row['expanded_bytes'], 'Expanded bytes differ from original size evidence')
        expected_tree = gate.expected_tree(document)
        context['inventory_entries'] = len(expected_tree)
        context['archive_bytes'] = row['inner_zip_bytes']; context['expanded_bytes'] = row['expanded_bytes']
        # No second parts copy: upload ONLY these allowlisted original prepared paths.
        delivery = prepared
        s.write_json(delivery/'producer-context.json', context)
        index = {'schema':1,'scope':'explicit-final-native-restorer-delivery','profile':args.profile,'files':delivery_files(delivery)}
        s.write_json(delivery/'delivery-index.json', index)
        record['context'] = context; record['delivery_index_sha256'] = s.sha(delivery/'delivery-index.json')
        record['delivery_expanded_bytes'] = sum(item['bytes'] for item in index['files'].values()) + (delivery/'delivery-index.json').stat().st_size
        s.need(s.file_record(outer) == record['original_download']['outer'], 'Original canonical outer changed')
        s.need(files(original) == expected, 'Original canonical members changed')
        checked_source(source)
        record['result'] = 'passed'
    except Exception as error:
        failure = error; record['error'] = f'{type(error).__name__}: {error}'
    finally:
        errors = []
        for name, check in (('source-after', lambda: checked_source(source)),
            ('project-notice-after', lambda: s.need(s.verify_project_notice(source, pin) == notice, 'Project notice changed')),
            ('original-outer-after', lambda: s.need(s.file_record(work/'original-canonical-outer.zip') == record['original_download']['outer'], 'Original outer changed')),
            ('original-members-after', lambda: s.need(files(work/'original') == record['original_members'], 'Original members changed'))):
            if name.startswith('original') and ('original_download' if name=='original-outer-after' else 'original_members') not in record: continue
            try: check()
            except Exception as error: errors.append({'check':name,'error':str(error)})
        record['post_run_errors'] = errors
        if errors:
            record['result'] = 'failed'
            if failure is None: failure = RuntimeError('Producer after-check failed')
        s.write_json(work/'producer-driver-report.json',record)
    if failure is not None: raise failure
    output_values({'context':context,'index_sha256':record['delivery_index_sha256'],
            'delivery_bytes':record['delivery_expanded_bytes'],'handoff_sha256':context['handoff_sha256']})


def safe_remove_verified_transport_outer(path, *, temp_root, evidence_path, extraction_root, index):
    """Only this job's exact redundant transport outer; never candidate inputs."""
    root = check_owned_work(temp_root)
    path = Path(path).absolute(); evidence_path = Path(evidence_path).absolute()
    extraction_root = Path(extraction_root).absolute()
    s.need(path.name == 'transport-outer.zip' and path.parent == root, 'Deletion outside exact private transfer directory')
    for item in (path, evidence_path):
        s.plain(item); s.need(item.stat().st_nlink == 1, 'Hardlinked deletion input/evidence forbidden')
    checked_ancestors(extraction_root)
    s.need(evidence_path.parent == root and evidence_path.name == 'transport-download-and-extract.json'
           and extraction_root == root/'delivery', 'Evidence/extraction outside private transfer directory')
    evidence = s.read_json(evidence_path)
    s.need(evidence.get('scope') == 'verified-redundant-transport-outer' and evidence.get('whole_outer_sha256_verified') is True
           and evidence.get('all_extracted_members_verified') is True and evidence.get('outer') == s.file_record(path)
           and evidence.get('index_sha256') == s.digest(s.canonical(index)), 'Missing complete persisted deletion evidence')
    s.need(files(extraction_root) == evidence.get('extracted_members'), 'Extracted members changed before deletion')
    s.need(evidence['extracted_members'] == {**index['files'], 'delivery-index.json': s.file_record(extraction_root/'delivery-index.json')},
           'Extraction not exact verified transfer')
    s.need(Path(extraction_root).resolve(strict=True).parent == root.resolve(strict=True) and evidence.get('upstream_artifact', {}).get('id'), 'Unbound extraction or upstream artifact')
    s.need(not (root/'transport-outer-removal.json').exists(), 'Removal audit already exists')
    # Evidence is fsynced before the only authorized removal.
    with evidence_path.open('r+b') as stream: os.fsync(stream.fileno())
    path.unlink()  # Single exact regular file only; no glob, recursive removal or system cleanup.
    s.write_json(root/'transport-outer-removal.json', {'scope':'redundant-local-transport-outer-only',
        'removed_path':str(path),'preserved_upstream_artifact':evidence['upstream_artifact'],
        'original_outer_identity':evidence['outer'],'verification_evidence':s.file_record(evidence_path),
        'parts_exe_source_and_reports_deleted':False})


def acquire_delivery(args, token, work):
    pin = load_pins()
    context = s.json_bytes(args.context.encode()); s.validate_context(context)
    s.need(context['profile'] == args.profile, 'Wrong delivery profile')
    metadata = artifact_metadata(args.artifact_id,token,name='restorer-final-delivery-'+args.profile,
        run_id=int(os.environ['GITHUB_RUN_ID']),commit=os.environ['GITHUB_SHA'])
    for name in ('archive_bytes','expanded_bytes','inventory_entries'):
        s.need(type(context.get(name)) is int and 0 < context[name] <= 1024**4, 'Missing/bad producer size bound')
    row = pin['profiles'][args.profile]
    s.need(context['archive_bytes'] == row['inner_zip_bytes'] and context['expanded_bytes'] == row['expanded_bytes']
           and context['inventory_entries'] <= 1000000, 'Producer size differs from fixed archive')
    expected_bytes = args.delivery_bytes
    s.need(0 < expected_bytes < 16 * 1024 ** 3, 'Delivery expansion bound')
    extra = context['archive_bytes'] + context['expanded_bytes'] + 65536*context['inventory_entries'] if args.action=='consume' else 0
    budget = s.space(work, max(metadata['size_in_bytes'] + expected_bytes, expected_bytes + extra) + s.RESERVE)
    outer=work/'transport-outer.zip'; download=download_artifact(metadata,outer,token)
    with zipfile.ZipFile(outer) as archive:
        row=archive.getinfo('delivery-index.json'); s.need(row.file_size <= 2*1024**2, 'Delivery index bound')
        raw=archive.read(row)
    s.need(s.digest(raw)==args.index_sha256,'Independent producer delivery-index SHA mismatch')
    index=s.json_bytes(raw)
    s.need(index.get('schema')==1 and index.get('scope')=='explicit-final-native-restorer-delivery' and index.get('profile')==args.profile,'Wrong delivery index')
    expected={**index['files'],'delivery-index.json':{'bytes':len(raw),'sha256':s.digest(raw)}}
    s.need(sum(row['bytes'] for row in expected.values())==expected_bytes,'Delivery bytes differ from producer')
    extracted=work/'delivery'; s.unpack(outer,extracted,limit=expected_bytes,expected=expected)
    s.need(s.read_json(extracted/'producer-context.json')==context,'Producer context changed in transfer')
    evidence={'scope':'verified-redundant-transport-outer','whole_outer_sha256_verified':True,
        'all_extracted_members_verified':True,'outer':download['outer'],'upstream_artifact':metadata,
        'index_sha256':s.digest(s.canonical(index)),'extracted_members':files(extracted),'budget':budget}
    evidence_path=work/'transport-download-and-extract.json';s.write_json(evidence_path,evidence)
    safe_remove_verified_transport_outer(outer,temp_root=work,evidence_path=evidence_path,extraction_root=extracted,index=index)
    if args.action == 'consume':
        s.write_json(work/'post-removal-restore-budget.json',s.space(work,extra+s.RESERVE))
    return extracted,context


def consumer(args):
    pin = load_pins()
    s.native_server(args.runner); source=checked_source(args.source)
    work=fresh_work(args.work)
    token=os.environ.pop('GITHUB_TOKEN',''); s.need(token,'Read-only artifact token required')
    delivery,context=acquire_delivery(args,token,work)
    row=pin['profiles'][args.profile]
    command=[sys.executable,'-B',str(source/'native/windows-restorer/release_gate.py'),'consume',
        '--handoff',str(delivery/'handoff.json'),'--handoff-sha256',context['handoff_sha256'],
        '--distribution',str(delivery/'build/distribution'),'--manifest',str(delivery/'volumes'/(row['inner_zip']+'.transport.json')),
        '--work',str(work/'acceptance'),'--runner',args.runner,'--application-source',str(source)]
    s.write_json(work/'consume-command.json',{'command':command,'producer_context':context})
    try:
        with (work/'consume.log').open('xb') as log:
            subprocess.run(command,cwd=work,env=s.clean_environment(),stdout=log,stderr=subprocess.STDOUT,timeout=12000,check=True)
    finally:
        report=work/'acceptance/consumer-report.json'
        if report.is_file():output_values({'report_sha256':s.sha(report)})
    result=s.read_json(work/'acceptance/consumer-report.json');gate=gate_at(source)
    gate.validate_consumer(result,s.read_json(delivery/'handoff.json'),args.runner)
    s.need(result['exe_sha256']==context['original_exe_sha256'],'Consumer used another EXE')


def validate_join(gate,handoff,consumers,rebuild,context):
    s.validate_source_report(rebuild,context)
    pin = load_pins(); row = pin['profiles'][context['profile']]
    archive = handoff.get('archive', {})
    s.need(archive.get('sha256') == row['inner_zip_sha256']
           and archive.get('identity', {}).get('commit') == pin['commit']
           and archive.get('identity', {}).get('version') == pin['version']
           and archive.get('identity', {}).get('build_profile') == context['profile'], 'Joined archive differs from explicit final input')
    s.need(len(consumers)==2 and {row.get('runner') for row in consumers}==set(s.RUNNERS),'Two distinct Server consumer reports required')
    for row in consumers:
        gate.validate_consumer(row,handoff,row['runner'])
        s.need(row.get('handoff_sha256')==context['handoff_sha256'] and row.get('exe_sha256')==context['original_exe_sha256'], 'Same-EXE/handoff binding failed')
    s.need(consumers[0]['restored_before_launcher']==consumers[1]['restored_before_launcher'],'Cross-Server payload differs')
    s.need(handoff['exe_sha256']==context['original_exe_sha256'] and handoff['source_zip_sha256']==context['source_zip_sha256'],'Producer source/EXE mismatch')
    return {'schema':1,'scope':'same-exe-two-server-and-independent-source-only-rebuild-not-release',
        'result':'passed','profile':context['profile'],'producer_context':context,'collector_rehashed_all_parts':True,
        'legacy_rebuild_transport_proof_claimed':False,'rebuilt_exe_not_used_as_delivery':True,
        'gpu_inference_verified':False,'windows_10_11_verified':False}


def collector(args):
    pin = load_pins()
    source=checked_source(args.source);work=fresh_work(args.work)
    token=os.environ.pop('GITHUB_TOKEN','');s.need(token,'Read-only artifact token required')
    config=s.read_json(args.collection); gate=gate_at(source); results=[]
    s.need(set(config)==set(pin['profiles']),'Both explicitly bound final profiles required')
    # Conservative: reserve both profiles' retained parts/distributions plus the
    # larger temporary outer. Only each proven redundant transport outer is removed.
    required=s.RESERVE
    metadata={}
    for profile,row in config.items():
        metadata[profile]=artifact_metadata(row['artifact_id'],token,name='restorer-final-delivery-'+profile,
            run_id=int(os.environ['GITHUB_RUN_ID']),commit=os.environ['GITHUB_SHA'])
        required+=row['delivery_bytes']
    required+=max(row['size_in_bytes'] for row in metadata.values())
    s.write_json(work/'collector-budget.json',s.space(work,required))
    for profile,row in config.items():
        child=fresh_work(work/profile)
        delivery_args=argparse.Namespace(action='collect',profile=profile,context=json.dumps(row['context']),
            artifact_id=row['artifact_id'],delivery_bytes=row['delivery_bytes'],index_sha256=row['index_sha256'])
        delivery,context=acquire_delivery(delivery_args,token,child)
        handoff,document=gate.load_handoff(delivery/'handoff.json',context['handoff_sha256'],delivery/'build/distribution',
            delivery/'volumes'/(pin['profiles'][profile]['inner_zip']+'.transport.json'))
        gate.validate_build(s.read_json(delivery/'build/distribution/build-record.json'))
        s.validate_compile(s.read_json(delivery/'build/distribution/build-record.json'),expected_exe=context['original_exe_sha256'])
        source_git_membership=verify_source_zip_git_membership(source,delivery/'build/distribution/restorer-source.zip')
        consumers=[];anchors=[]
        for runner in s.RUNNERS:
            path=args.reports/(profile+'-'+runner)/'consumer-report.json'
            report,report_pin=gate.read_bound_report(path,row['consumer_report_sha256'][runner]);consumers.append(report);anchors.append({'runner':runner,**report_pin})
        path=args.reports/(profile+'-source')/'source-only-report.json'
        rebuild,rebuild_pin=gate.read_bound_report(path,row['source_report_sha256'])
        s.validate_source_report(rebuild,context,path.parent)
        verify_rebuilt_map(source,path.parent/'rebuilt/link-map.txt',rebuild['rebuild_record']['restricted_zlib']['final_link_map'])
        result=validate_join(gate,handoff,consumers,rebuild,context)
        result.update(original_consumer_reports=anchors,original_source_report=rebuild_pin,original_archive=document['archive'])
        result['source_zip_git_membership']=source_git_membership
        # Original reports/inputs stay on disk; collector really rechecks volumes.
        gate.load_handoff(delivery/'handoff.json',context['handoff_sha256'],delivery/'build/distribution',
            delivery/'volumes'/(pin['profiles'][profile]['inner_zip']+'.transport.json'))
        for runner in s.RUNNERS:
            gate.read_bound_report(args.reports/(profile+'-'+runner)/'consumer-report.json',row['consumer_report_sha256'][runner])
        gate.read_bound_report(path,row['source_report_sha256'])
        artifact_rows = {}
        named = {'producer_raw':('restorer-final-producer-raw-'+profile,row['artifacts']['producer_raw']),
            'source':('restorer-final-source-'+profile,row['artifacts']['source']),
            'source_report':('restorer-final-report-'+profile+'-source',row['artifacts']['source_report']),
            'source_raw':('restorer-final-source-raw-'+profile,row['artifacts']['source_raw'])}
        for runner in s.RUNNERS:
            named[runner+'-report']=('restorer-final-report-'+profile+'-'+runner,row['artifacts'][runner]['report'])
            named[runner+'-raw']=('restorer-final-consumer-raw-'+profile+'-'+runner,row['artifacts'][runner]['raw'])
        for label,(name,identifier) in named.items():
            artifact_rows[label]=artifact_metadata(identifier,token,name=name,run_id=int(os.environ['GITHUB_RUN_ID']),commit=os.environ['GITHUB_SHA'])
        result['upstream_job_artifacts'] = artifact_rows
        s.write_json(child/'joined-report.json',result);results.append(result)
    s.write_json(work/'all-profiles-report.json',{'schema':1,'result':'passed','scope':'explicit-final-inputs-build-only-not-release','profiles':results})


def collection_plan(needs):
    load_pins()
    required = {'produce-'+short for short in ('cu118','cu126')} | {'rebuild-'+short for short in ('cu118','cu126')} | {
        'consume-'+short+'-'+year for short in ('cu118','cu126') for year in ('2022','2025')}
    s.need(set(needs) == required and all(row.get('result') == 'success' for row in needs.values()),
           'All eight actual upstream jobs must succeed; missing/failed/skipped is not success')
    result = {}
    for short,profile in (('cu118','paddle-cuda11.8'),('cu126','paddle-cuda12.6')):
        producer = needs['produce-'+short]['outputs']; rebuild = needs['rebuild-'+short]['outputs']
        context = s.json_bytes(producer['context'].encode()); s.validate_context(context)
        s.need(context['profile'] == profile and context['handoff_sha256'] == producer['handoff_sha256'], 'Wrong producer context output')
        s.hash_text(producer['index_sha256']); s.hash_text(rebuild['report_sha256'])
        def identity(value):
            s.need(isinstance(value,str) and value.isdecimal() and int(value)>0, 'Missing immutable job artifact ID')
            return int(value)
        consumers = {runner:needs['consume-'+short+'-'+runner[-4:]]['outputs'] for runner in s.RUNNERS}
        for row in consumers.values(): s.hash_text(row['report_sha256'])
        artifacts = {'producer_raw':identity(producer['raw_artifact_id']), 'source':identity(producer['source_artifact_id']), 'source_report':identity(rebuild['report_artifact_id']),
            'source_raw':identity(rebuild['raw_artifact_id']),
            **{runner:{'report':identity(row['report_artifact_id']),'raw':identity(row['raw_artifact_id'])} for runner,row in consumers.items()}}
        result[profile] = {'artifact_id':identity(producer['delivery_artifact_id']), 'delivery_bytes':int(producer['delivery_bytes']),
            'index_sha256':producer['index_sha256'],'context':context,'source_report_sha256':rebuild['report_sha256'],
            'consumer_report_sha256':{runner:row['report_sha256'] for runner,row in consumers.items()},'artifacts':artifacts}
    return result


def collect_from_environment(source):
    load_pins()
    root = fresh_work(Path(os.environ['RUNNER_TEMP'])/'restorer-final-collector-evidence')
    needs = s.json_bytes(os.environ['VSE_NEEDS'].encode())
    s.write_json(root/'original-needs-outputs.json', needs)
    report = {'scope':'eight-job-strict-collector','result':'failed'}
    try:
        config = collection_plan(needs); s.write_json(root/'collection-inputs.json',config)
        args = argparse.Namespace(source=source,work=root/'verification',collection=root/'collection-inputs.json',
            reports=Path(os.environ['RUNNER_TEMP'])/'restorer-final-reports')
        collector(args); report['result']='passed'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        s.write_json(root/'collector-status.json',report)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    canonical=sub.add_parser('canonical-source')
    canonical.add_argument('--source',type=Path,required=True);canonical.add_argument('--audit',type=Path,required=True)
    for action in ('produce','consume','collect'):
        p=sub.add_parser(action);p.add_argument('--source',type=Path,required=True);p.add_argument('--work',type=Path,required=True)
        if action!='collect':p.add_argument('--profile',choices=s.PROFILES,required=True)
        if action=='produce':p.add_argument('--acknowledge-license',action='store_true')
        if action=='consume':
            p.add_argument('--runner',choices=tuple(s.RUNNERS),required=True)
            for name in ('context','index-sha256'):p.add_argument('--'+name,required=True)
            for name in ('artifact-id','delivery-bytes'):p.add_argument('--'+name,type=int,required=True)
        if action=='collect':
            p.add_argument('--collection',type=Path,required=True);p.add_argument('--reports',type=Path,required=True)
    args=parser.parse_args()
    if args.action=='canonical-source':
        result=canonicalize_source_checkout(args.source);s.write_json(args.audit,result)
        print(json.dumps({'commit':result['commit'],'tree':result['tree'],'file_count':result['file_count'],
                          'source_files_written':result['source_files_written'],'network_build_or_dispatch':False}))
        return
    {'produce':producer,'consume':consumer,'collect':collector}[args.action](args)


if __name__=='__main__':main()
