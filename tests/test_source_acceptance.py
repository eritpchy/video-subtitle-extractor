"""Fully synthetic source receipts; never real inference or GPU acceptance."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import source_acceptance as gate
from scripts import source_ci_steps as runner
from backend.tools import python_coreml_ocr as impl
from tests.test_check_paddle_models import synthetic_report as paddle_fixture

EXPECTED = gate.expected_identity('a' * 40, 'owner/repo', 111, 1)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def inventory(path):
    return {'bytes': path.stat().st_size, 'sha256': gate._sha(path)}


def record(case, state, provider, window, *, pid=123, engine=1):
    tier, operation = case['tier'], case['operation']
    rec = case['rec_model'] or f'PP-OCRv6_{tier}_rec'
    fixed = gate._json(gate.ROOT / 'native/ocr/model-source-pins.json')
    hashes = {row['name']: row['onnx_sha256'] for row in fixed['models']}
    key = 'darwin-arm64-cp312' if state['identity']['profile'].startswith('macos') else 'linux-x86_64-cp312'
    wheel = gate._json(gate.ROOT / 'backend/tools/python_coreml_runtime.json')['wheels'][key]
    base = window['started_utc_ns'] + engine * 100
    def clock(t): return {'process_pid': pid, 'utc_ns': base + t, 'monotonic_ns': base + t}
    empty = not (case['actual'][1] if operation == 'ocr' else case['actual'])
    sessions = {'det': None, 'rec': None}
    for role, shape, sid in [('det', [1, 3, 64, 320], 1), ('rec', [1, 3, 48, 320], 2)]:
        if role == 'rec' and (operation == 'detect' or empty): continue
        coreml = provider == 'coreml'
        child = {'shape_session_id': sid, 'input_shape': shape,
            'free_dimension_overrides': impl._shape_bindings(role, shape, 6), 'require_static_input_shapes': True,
            'request_run_count': 1, 'session_run_count': 1, 'evidence_reused': False,
            'profile_started': clock(12), 'profile_ended': clock(22),
            'source': 'ort_first_run_profile' if coreml else 'explicit_cpu_session',
            'profile_sha256': '1' * 64 if coreml else None,
            'kernel_events': {impl.COREML: int(coreml), impl.CPU: int(coreml)},
            'coreml_ep_executed': coreml, 'cpu_partition_observed': coreml, 'gpu_execution_verified': False}
        sessions[role] = {'model': f'PP-OCRv6_{tier}_det' if role == 'det' else rec,
            'status': 'executed', 'capture': 'per_shape_first_successful_run', 'request_run_count': 1,
            'session_run_count': 1, 'cache_capacity': 2 if role == 'det' else 4,
            'cached_shape_session_ids': [sid], 'shape_sessions': [child], 'gpu_execution_verified': False}
    return {'schema': 1, 'scope': 'python-source-inference', 'backend': 'python-coreml', 'provider': provider,
        'process_pid': pid, 'engine_id': engine, 'request_id': 1,
        'interpreter': state['environment']['interpreter'], 'interpreter_sha256': state['environment']['interpreter_sha256'],
        'source_files': impl._source_identity(),
        'runtime': {'package_root': str(gate._path(state, state['environment']['prefix']) / 'site-packages/onnxruntime'),
            'binding_path': str(gate._path(state, state['environment']['prefix']) / 'site-packages/onnxruntime/capi/binding.so'), 'version': impl.ORT_VERSION, 'wheel_filename': wheel['filename'], 'wheel_sha256': wheel['sha256'],
            'native_files': wheel['native_files'], 'telemetry_disabled_before_import': True,
            'available_providers': [impl.COREML, impl.CPU] if provider == 'coreml' else [impl.CPU]},
        'started': clock(10), 'ended': clock(30),
        'request': {'tier': tier, 'rec_model': rec, 'batch_size': 6, 'cpu_threads': 2, 'operation': operation},
        'result_empty': empty, 'model_sha256': {name: hashes[name] for name in
            ([f'PP-OCRv6_{tier}_det'] if operation == 'detect' else [f'PP-OCRv6_{tier}_det', rec])},
        'input_encoded_png_sha256': 'b' * 64, 'input_encoded_png_bytes': 1234,
        'sessions': sessions, 'gpu_execution_verified': False, 'internal_coreml_precision_verified': False}


def image_report(state, provider, window):
    cases = gate.frozen.load_cases(gate.ROOT / 'native/ocr/tests/evidence')
    for row in cases:
        row['actual'] = copy.deepcopy(row['expected'])
        if row['operation'] == 'ocr': row.update(gate.frozen.compare_ocr(row['actual'], row['expected']))
        else: row['passed'] = True
        r = record(row, state, provider, window, engine=row['id'] + 1)
        row['request_evidence'] = r
        row['executed_models'] = sorted(s['model'] for s in r['sessions'].values() if s)
    runtime = cases[0]['request_evidence']['runtime']
    pins = gate._json(gate.ROOT / 'native/ocr/model-source-pins.json')
    model_files = {'model-source-pins.json': gate.frozen.SOURCE_PINS_SHA256, 'model-manifest.json': 'f' * 64,
        **pins['config_sha256'], **{'models/' + r['name'] + '_infer.onnx': r['onnx_sha256'] for r in pins['models']}}
    return {'synthetic_unit_fixture': True, 'schema': 1, 'scope': 'python-source-correctness', 'backend': 'python-coreml',
        'commit': EXPECTED['commit'], 'provider': provider, 'case_set': 'all', 'result': 'passed',
        'network_attempts': [], 'subprocess_attempts': [], 'native_worker_used': False, 'gpu_execution_verified': False,
        'internal_coreml_precision_verified': False, 'final_package_accepted': False, 'models_unchanged': True,
        'source_unchanged': True, 'paddle_imported': False, 'runtime_files_unchanged': True, 'loader_environment_clean': True,
        'confidence_tolerance': gate.frozen.TOLERANCE, 'interpreter': state['environment']['interpreter'],
        'cases': cases, 'covered_models': sorted(gate.frozen.ALL_MODELS), 'source_files': impl._source_identity(),
        'runtime': runtime, 'runtime_native_files_before': runtime['native_files'], 'runtime_native_files_after': runtime['native_files'],
        'model_files': model_files}


def make(root, profile):
    root.mkdir(parents=True)
    windows = profile.startswith('windows-')
    P = gate.PureWindowsPath if windows else gate.PurePosixPath
    checkout, evidence, venv = [P('C:/synthetic' if windows else '/synthetic') / x for x in ('checkout', 'evidence', 'venv')]
    target = runner.PROFILES[profile]
    environment = {'python': [3, 12], 'user_site_enabled': False, 'venv_created_new': True,
        'system_site_packages': False, 'loader_overrides': {}, 'interpreter_sha256': 'c' * 64,
        'pyvenv_cfg_sha256': 'd' * 64, 'prefix': str(venv), 'base_prefix': str(venv.parent / 'base'),
        'interpreter': str(venv / ('Scripts/python.exe' if windows else 'bin/python'))}
    state = {'schema': 1, 'identity': {**EXPECTED, 'profile': profile, 'job': 'synthetic', 'runner_os': target[0],
        'runner_arch': target[1], 'runner': target[2]},
        'checkout': {'commit': EXPECTED['commit'], 'dirty': False, 'files': {n: gate._sha(gate.ROOT / n) for n in gate.contract_files()}},
        'environment': environment, 'models': str(checkout / 'build/models'), 'steps': [],
        'paths': {'checkout': str(checkout), 'evidence': str(evidence), 'models': str(checkout / 'build/models'), 'venv': str(venv)}}
    names = ['venv-create', 'venv-probe', 'pip-install', 'pip-check', 'source-default', 'models-verify', 'vsf-verify', 'gui-smoke', 'cli-version', 'cli-help']
    if windows: names.append('cuda-environment')
    names += ['image-cpu'] + (['image-coreml'] if profile.startswith('macos') else []) + ['video']
    default = {'source_default': 'paddle', 'custom_worker_files': [], 'interpreter': environment['interpreter'], 'scope': 'synthetic'}
    for i, name in enumerate(names):
        argv = [str(venv.parent / 'base/python'), '-m', 'venv', str(venv)] if name == 'venv-create' else [environment['interpreter'], '-I', '-c', runner.PROBE] if name == 'venv-probe' else runner.command(state, evidence, name)
        row = {'name': name, 'argv': argv, 'returncode': 0, 'started_utc_ns': (i + 1) * 1000000, 'ended_utc_ns': (i + 2) * 1000000 - 1}
        for stream in ('stdout', 'stderr'):
            name_log = name + '.' + stream + '.log'; path = root / name_log
            if stream == 'stdout' and name in ('venv-probe', 'source-default'): write(path, environment if name == 'venv-probe' else default)
            else: path.write_bytes(b'synthetic command log\n')
            row[stream] = {'path': name_log, **inventory(path)}
        state['steps'].append(row)
    write(root / 'steps.json', state); write(root / 'source-default.json', default)
    steps = {r['name']: r for r in state['steps']}
    if windows:
        report = paddle_fixture(); report.update(commit=None, version=None, build_profile=None,
            python=environment['interpreter'], app_root=str(checkout), model_root=state['models'],
            runtime_versions={'paddle': '3.3.1', 'paddleocr': '3.7.0'},
            production_modules={str(checkout / (n.replace('.', '/') + '.py')): gate._sha(gate.ROOT / (n.replace('.', '/') + '.py')) for n in gate.frozen.PRODUCTION_MODULES})
        report['device']['compiled_with_cuda'] = True
        from scripts.paddle_package_models import _model_path
        pins = gate._json(gate.ROOT / 'native/ocr/model-source-pins.json')
        report['model_files'] = {_model_path(r['name']) + '/' + n: {'sha256': d, 'bytes': 1} for r in pins['models'] for n, d in r['files'].items()}
    else: report = image_report(state, 'cpu', steps['image-cpu'])
    write(root / 'cpu-all.json', report)
    if profile.startswith('macos'): write(root / 'coreml-all.json', image_report(state, 'coreml', steps['image-coreml']))
    install(root, state)
    video(root, state, steps['video'])
    return root, state


def install(root, state):
    profile = state['identity']['profile']; windows = profile.startswith('windows')
    if windows:
        cuda = profile.removeprefix('windows-cuda'); pin, wheels = gate.cuda_checker.load_profile(cuda)
        installs = [{'metadata': {'name': name, 'version': '3.3.1' if name == 'paddlepaddle-gpu' else '12.6.85'},
                     'download_info': {'url': row['url'], 'archive_info': {'hashes': {'sha256': row['sha256']}}},
                     'is_direct': True, 'requested': True} for name, row in wheels.items()]
    else:
        key = 'darwin-arm64-cp312' if profile.startswith('macos') else 'linux-x86_64-cp312'
        pin = gate._json(gate.ROOT / 'backend/tools/python_coreml_runtime.json')['wheels'][key]
        text = (gate.ROOT / 'requirements-coreml-inference.txt').read_text()
        url = next(u.split('#')[0] for u in gate.re.findall(r'onnxruntime @ (https://\S+)', text) if '/' + pin['filename'] + '#' in u)
        installs = [{'metadata': {'name': 'onnxruntime', 'version': '1.31.0'}, 'download_info': {'url': url,
                     'archive_info': {'hashes': {'sha256': pin['sha256']}}}, 'is_direct': True, 'requested': True}]
    write(root / 'pip-install.json', {'install': installs})
    if windows:
        env = state['environment']; packages = {'paddlepaddle-gpu': '3.3.1', 'paddleocr': '3.7.0', 'paddlex': '3.7.2',
            **{k: '6.12.0' for k in ('pyside6', 'pyside6-essentials', 'pyside6-addons', 'shiboken6')}}
        if cuda == '12.6': packages['nvidia-nvjitlink-cu12'] = '12.6.85'
        runtime = {'platform': 'win32', 'architecture': 'amd64', 'implementation': 'CPython', 'python': [3,12],
            'user_site_enabled': False, 'paddle_version': '3.3.1', 'compiled_with_cuda': True, 'compiled_cuda_version': cuda,
            'actual_device': 'cpu', **{k: env[k] for k in ('interpreter', 'prefix', 'base_prefix')},
            'paddle_origin': str(gate.PureWindowsPath(env['prefix']) / 'Lib/site-packages/paddle/__init__.py'), 'distributions': packages}
        write(root / 'environment.json', {'schema':1, 'scope':'python-cuda-source-environment', 'cuda':cuda,'result':'passed',
            'gpu_inference_verified':False,'native_worker_used':False,'ocr_cases_verified':False,'final_package_accepted':False,
            'source_wheels':gate.cuda_checker.validate_install_report({'install':installs},wheels), 'reviewed_wheel_provenance':pin,
            'pip_report_sha256':gate._sha(root/'pip-install.json'), 'runtime':runtime})
    from backend.tools.videosubfinder import release_asset, VSF_RELEASE_BASE
    platform, arch = ('windows','x64') if windows else ('macos','arm64') if profile.startswith('macos') else ('linux','x64')
    asset = release_asset(platform,arch)
    write(root/'vsf-install.json', {'version':'6.10.7','platform':platform,'arch':arch,'cuda':False,'asset':asset,
        'source':VSF_RELEASE_BASE+'/v6.10.7/'+asset,'release_locked':True,'sha256':'f'*64,
        'sha256sums_sha256':gate._json(gate.ROOT/'backend/tools/videosubfinder_release.json')['sha256sums_sha256']})


def video(root, state, window):
    profile=state['identity']['profile']; windows=profile.startswith('windows')
    providers=('cpu','coreml') if profile.startswith('macos') else ('cpu',)
    cases=[]; env=state['environment']
    if not windows:
        path=root/'video/same input video.mp4';path.parent.mkdir();path.write_bytes(b'synthetic video')
    for i,mode in enumerate(gate.MODES):
        for j,provider in enumerate(providers):
            folder=root/'video'/(mode if windows else mode+'-'+provider);folder.mkdir(parents=True)
            source=folder/'input video.mp4' if windows else root/'video/same input video.mp4'
            if windows:source.write_bytes(b'synthetic video')
            srt=folder/'recognized.srt';srt.write_text('1\n00:00:01,000 --> 00:00:03,000\nHELLO WORLD\n\n2\n00:00:04,000 --> 00:00:06,000\nSECOND LINE\n')
            row={'result':'passed','mode':mode,'cpu_forced':provider=='cpu',
                 'requested_provider' if windows else 'provider':provider,
                 'gpu_inference_verified' if windows else 'gpu_execution_verified':False,
                 'command':[env['interpreter'],'-m','backend.cli',
                     str(gate._path(state,state['paths']['evidence']) / ('video/'+mode+'/input video.mp4' if windows else 'video/same input video.mp4')),
                     '--output',str(gate._path(state,state['paths']['evidence']) / ('video/'+(mode if windows else mode+'-'+provider)) / 'recognized.srt'),
                     '--language','en','--mode',mode,*(['--cpu'] if provider=='cpu' else []),
                     '--threads','2','--area','0.6,1,0,1','--overwrite','--no-word-segmentation'],
                 **gate._srt(srt),'srt_sha256':gate._sha(srt),'input_video_sha256':gate._sha(source)}
            if not windows:
                tier='medium' if mode=='accurate' else 'small'
                r=record({'tier':tier,'operation':'ocr','rec_model':f'PP-OCRv6_{tier}_rec','actual':[[1],[1]]},state,provider,window,pid=500+i*2+j)
                name='python-ocr-{}-1-1.json'.format(r['process_pid']);path=folder/'python-requests'/name;write(path,r)
                row.update(timing_is_benchmark=False,executed_models=sorted(s['model'] for s in r['sessions'].values()),
                           python_requests=[{'path':name,**inventory(path)}])
            write(folder/'report.json',row);cases.append(row)
    report={'result':'passed','cases':cases}
    if windows:report.update(modes_requested=list(gate.MODES),cpu_forced=True,requested_provider='cpu',gpu_inference_verified=False)
    else:report.update(schema=1,scope='python-source-video-acceptance',backend='python-coreml',providers=list(providers),modes=list(gate.MODES),
        gpu_execution_verified=False,final_package_accepted=False,timing_is_benchmark=False,commit=EXPECTED['commit'],
        interpreter=env['interpreter'],interpreter_sha256=env['interpreter_sha256'],input_video_sha256=cases[0]['input_video_sha256'])
    write(root/'video/report.json',report)


@pytest.mark.parametrize('profile',gate.PROFILES)
def test_complete_synthetic_source_profile_passes_without_claiming_package(tmp_path,profile):
    root,state=make(tmp_path/profile,profile)
    result=gate.produce(root,expected=EXPECTED)
    assert result['checks']==gate.checks(profile)
    assert result['final_package_accepted'] is False and result['gpu_inference_verified'] is False


def test_pure_stdlib_import_without_runtime_initialization():
    code=f'import sys;sys.path.insert(0,{str(gate.ROOT)!r});from scripts import source_acceptance;assert not set(sys.modules)&{{"numpy","onnxruntime","paddle","PySide6","packaging"}}'
    subprocess.run([sys.executable,'-I','-S','-c',code],check=True)


@pytest.fixture
def linux(tmp_path):
    return make(tmp_path/'evidence','linux-ort-cpu')[0]


def reject(root):
    with pytest.raises((ValueError, KeyError, TypeError, OSError, IndexError)):
        gate.produce(root,expected=EXPECTED)
    assert not (root/gate.RECEIPT).exists()


@pytest.mark.parametrize('mutation', ['old_commit','other_run','attempt','profile','dirty','source_hash',
    'base_venv','user_site','system_site','loader','interpreter','probe','missing_step','duplicate_step','failed_step',
    'echo_command','argv_cpu','out_of_order','log_hash','log_bytes','source_default'])
def test_steps_are_not_success_keyword_or_old_run_evidence(linux,mutation):
    path=linux/'steps.json';state=gate._json(path)
    if mutation=='old_commit': state['identity']['commit']='b'*40
    elif mutation=='other_run':state['identity']['run_id']+=1
    elif mutation=='attempt':state['identity']['run_attempt']+=1
    elif mutation=='profile':state['identity']['profile']='macos-ort-cpu-coreml'
    elif mutation=='dirty':state['checkout']['dirty']=True
    elif mutation=='source_hash':state['checkout']['files']['backend/cli.py']='f'*64
    elif mutation=='base_venv':state['environment']['base_prefix']=state['environment']['prefix']
    elif mutation=='user_site':state['environment']['user_site_enabled']=True
    elif mutation=='system_site':state['environment']['system_site_packages']=True
    elif mutation=='loader':state['environment']['loader_overrides']={'LD_PRELOAD':'injected'}
    elif mutation=='interpreter':state['environment']['interpreter']='/host/python'
    elif mutation=='probe':(linux/'venv-probe.stdout.log').write_text('{}')
    elif mutation=='missing_step':state['steps'].pop()
    elif mutation=='duplicate_step':state['steps'][-1]=state['steps'][-2]
    elif mutation=='failed_step':state['steps'][-1]['returncode']=1
    elif mutation=='echo_command':state['steps'][-1]['argv']=['echo','passed']
    elif mutation=='argv_cpu':state['steps'][-2]['argv']+=['--cpu']
    elif mutation=='out_of_order':state['steps'][-1]['started_utc_ns']=1
    elif mutation=='log_hash':state['steps'][-1]['stdout']['sha256']='f'*64
    elif mutation=='log_bytes':state['steps'][-1]['stdout']['bytes']+=1
    else:write(linux/'source-default.json',{'source_default':'native','custom_worker_files':[]})
    write(path,state);reject(linux)


@pytest.mark.parametrize('mutation',['missing_mode','fake_expected','duplicate','summary_only','failed_case','old_commit','native_scope',
    'gpu','profile_only','source_modules','runtime_before','runtime_after','runtime_platform','model_inventory','wrong_input',
    'wrong_case_request','old_execution','runtime_outside_venv','interpreter','empty_result','replayed_auto'])
def test_image_consumer_recomputes_full_frozen_and_actual_record_contract(linux,mutation):
    path=linux/'cpu-all.json';report=gate._json(path);case=report['cases'][0];record=case['request_evidence']
    if mutation=='missing_mode':report['cases']=report['cases'][:112]
    elif mutation=='fake_expected':case['expected']=[[],[]]
    elif mutation=='duplicate':report['cases'][-1]=case
    elif mutation=='summary_only':report['cases']=[];report['passed_cases']=168
    elif mutation=='failed_case':case['passed']=False
    elif mutation=='old_commit':report['commit']='b'*40
    elif mutation=='native_scope':report['scope']='final-bundle'
    elif mutation=='gpu':record['gpu_execution_verified']=True
    elif mutation=='profile_only':report['case_set']='models'
    elif mutation=='source_modules':record['source_files']={}
    elif mutation=='runtime_before':report['runtime_native_files_before']={}
    elif mutation=='runtime_after':report['runtime_native_files_after']={}
    elif mutation=='runtime_platform':report['runtime']['wheel_filename']='other.whl'
    elif mutation=='model_inventory':report['model_files']={}
    elif mutation=='wrong_input':record['input_encoded_png_sha256']='not-a-hash'
    elif mutation=='wrong_case_request':record['request']['tier']='medium'
    elif mutation=='old_execution':record['started']['utc_ns']=1;record['ended']['utc_ns']=2
    elif mutation=='runtime_outside_venv':record['runtime']['package_root']='/host/onnxruntime'
    elif mutation=='interpreter':record['interpreter']='/host/python'
    elif mutation=='empty_result':record['result_empty']=True
    else:report['cases'][112]['request_evidence']=copy.deepcopy(case['request_evidence'])
    write(path,report);reject(linux)


@pytest.mark.parametrize('mutation',['srt','video','cpu_flag','missing_auto','request_missing','request_hash','wrong_tier','scope','pip','vsf'])
def test_actual_video_install_and_vsf_bytes_are_required(linux,mutation):
    if mutation=='srt':(linux/'video/fast-cpu/recognized.srt').write_text('wrong')
    elif mutation=='video':(linux/'video/same input video.mp4').write_bytes(b'different')
    elif mutation in ('cpu_flag','missing_auto','scope'):
        p=linux/'video/report.json';r=gate._json(p)
        if mutation=='cpu_flag':r['cases'][0]['cpu_forced']=False
        elif mutation=='missing_auto':r['cases'].pop()
        else:r['scope']='final-package'
        write(p,r)
    elif mutation=='request_missing':next((linux/'video/fast-cpu/python-requests').iterdir()).unlink()
    elif mutation in ('request_hash','wrong_tier'):
        p=next((linux/'video/fast-cpu/python-requests').iterdir());r=gate._json(p)
        if mutation=='request_hash':r['gpu_execution_verified']=True
        else:r['request']['tier']='medium'
        write(p,r)
    elif mutation=='pip':p=linux/'pip-install.json';r=gate._json(p);r['install'][0]['download_info']['archive_info']['hashes']['sha256']='f'*64;write(p,r)
    else:p=linux/'vsf-install.json';r=gate._json(p);r['version']='6.10.6';write(p,r)
    reject(linux)


@pytest.mark.parametrize('mutation',['cpu_paddle','partial_qt','wrong_cuda','device_gpu','host_runtime','bundle_scope','image_cpu_build','image_models','source_modules'])
def test_windows_source_does_not_impersonate_cuda_gpu_or_bundle(tmp_path,mutation):
    root,state=make(tmp_path/'evidence','windows-cuda12.6')
    path=root/'environment.json';report=gate._json(path);runtime=report['runtime']
    if mutation=='cpu_paddle':runtime['distributions']['paddlepaddle']='3.3.1'
    elif mutation=='partial_qt':del runtime['distributions']['pyside6-addons']
    elif mutation=='wrong_cuda':runtime['compiled_cuda_version']='11.8'
    elif mutation=='device_gpu':runtime['actual_device']='gpu:0'
    elif mutation=='host_runtime':runtime['paddle_origin']='C:/host/paddle/__init__.py'
    elif mutation=='bundle_scope':report['final_package_accepted']=True
    else:
        path=root/'cpu-all.json';report=gate._json(path)
        if mutation=='image_cpu_build':report['device']['compiled_with_cuda']=False
        elif mutation=='image_models':report['model_files']={}
        else:report['production_modules']={}
    write(path,report);reject(root)


def test_stale_success_receipt_is_removed_on_failure(linux):
    gate.produce(linux,expected=EXPECTED)
    (linux/'cpu-all.json').write_text('{}')
    reject(linux)


def test_symlinked_evidence_never_reads_outside_root(linux,tmp_path):
    p=linux/'cpu-all.json';outside=tmp_path/'outside';p.rename(outside)
    try:p.symlink_to(outside)
    except OSError:pytest.skip('Symlink unavailable')
    reject(linux)


def test_evidence_mutation_during_acceptance_fails(linux,monkeypatch):
    original=gate._video
    def change(*args,**kwargs):
        original(*args,**kwargs);(linux/'cli-help.stdout.log').write_bytes(b'changed')
    monkeypatch.setattr(gate,'_video',change);reject(linux)


def test_all_four_raw_profiles_are_revalidated_and_external_aggregate_hash_required(tmp_path):
    root=tmp_path/'matrix'
    for profile in gate.PROFILES:
        directory,_=make(root/profile,profile);gate.produce(directory,expected=EXPECTED)
    output=tmp_path/'source-set.json'
    result=gate.aggregate(root,expected=EXPECTED,output=output)
    assert set(result['profiles'])==set(gate.PROFILES)
    assert gate.verify_aggregate(output,expected=EXPECTED,sha256=gate._sha(output))==result
    with pytest.raises(ValueError):gate.verify_aggregate(output,expected=EXPECTED,sha256='0'*64)
    with pytest.raises(ValueError):gate.verify_aggregate(output,expected={**EXPECTED,'run_id':112},sha256=gate._sha(output))
    altered=copy.deepcopy(result);altered['profiles'].pop('windows-cuda11.8');write(output,altered)
    with pytest.raises(ValueError):gate.verify_aggregate(output,expected=EXPECTED,sha256=gate._sha(output))
    (root/'linux-ort-cpu/cpu-all.json').write_text('{}')
    with pytest.raises((ValueError,KeyError)):gate.aggregate(root,expected=EXPECTED,output=tmp_path/'bad.json')
    assert not (tmp_path/'bad.json').exists()


def test_missing_and_extra_profile_directory_fail(tmp_path):
    root=tmp_path/'matrix';root.mkdir()
    with pytest.raises(ValueError):gate.aggregate(root,expected=EXPECTED,output=tmp_path/'out.json')
    for profile in gate.PROFILES:(root/profile).mkdir()
    (root/'old-run').mkdir()
    with pytest.raises(ValueError):gate.aggregate(root,expected=EXPECTED,output=tmp_path/'out.json')
