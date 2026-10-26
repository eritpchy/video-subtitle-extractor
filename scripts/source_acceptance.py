"""Pure-stdlib Python SOURCE evidence gate, separate from distribution/GPU proof.

produce and aggregate independently inspect raw model/video/step evidence.
verify authenticates an externally SHA-bound aggregate reference only; it does
not pretend to recheck raw evidence that was not downloaded. CI receipts are
unsigned execution records, not cryptographic or physical-GPU attestations.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import check_paddle_models as frozen
from scripts import source_ci_steps as runner
from scripts import check_source_cuda_environment as cuda_checker
from backend.tools.python_coreml_ocr import validate_python_coreml_records

PROFILES = ('linux-ort-cpu', 'macos-ort-cpu-coreml', 'windows-cuda11.8', 'windows-cuda12.6')
SCOPE = 'Python source acceptance only; not a self-contained distribution, physical GPU proof, or signed attestation.'
RECEIPT = 'source-acceptance.json'
MODES = ('fast', 'accurate', 'auto')
MAX_JSON = 64 * 1024 * 1024


def contract_files():
    return ('.github/workflows/python-source.yml', 'scripts/source_acceptance.py', 'scripts/source_ci_steps.py',
            'scripts/check_python_coreml.py', 'scripts/e2e_python_coreml.py', 'scripts/check_paddle_models.py',
            'scripts/check_source_cuda_environment.py', 'scripts/e2e_smoke.py', 'scripts/prepare_source_models.py',
            'scripts/paddle_package_models.py', 'scripts/install_videosubfinder.py',
            'backend/tools/python_coreml_ocr.py', 'backend/tools/python_coreml_runtime.json',
            'backend/tools/directml_ocr.py', 'backend/tools/native_ocr.py', 'backend/tools/ocr.py',
            'backend/tools/subtitle_detect.py', 'backend/tools/paddle_model_config.py',
            'backend/tools/hardware_accelerator.py', 'backend/tools/videosubfinder.py',
            'backend/tools/videosubfinder_release.json', 'backend/main.py', 'backend/cli.py', 'gui.py',
            'requirements-coreml-inference.txt', 'requirements-source-common.txt',
            'requirements-source-cuda118.txt', 'requirements-source-cuda126.txt',
            'docs/audits/source-cuda-wheel-pins.json', 'native/ocr/model-source-pins.json',
            'native/ocr/tests/evidence/fixture-manifest.json', 'native/ocr/tests/goldens/smoke.json')


def _fail(message):
    raise ValueError(message)


def _hex(value, n=64):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(n) + '}', value) is not None


def _positive(value):
    return type(value) is int and value > 0


@contextmanager
def _opened(path):
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400:
        _fail('Expected non-reparse regular evidence file')
    def stable(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
                getattr(info, 'st_birthtime_ns', None))
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if stable(before) != stable(opened):
            _fail('Evidence changed while opening')
        yield stream
        finished, after = os.fstat(stream.fileno()), path.lstat()
        if (stable(opened) != stable(finished) or opened.st_ctime_ns != finished.st_ctime_ns
                or stable(before) != stable(after) or before.st_ctime_ns != after.st_ctime_ns
                or stable(after) != stable(finished)):
            _fail('Evidence changed while reading')


def _sha(path):
    with _opened(path) as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()


def _decode_json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('Duplicate source evidence JSON key')
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda value: _fail('Nonfinite JSON evidence'))


def _json(path):
    with _opened(path) as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        _fail('Oversized source JSON evidence')
    return _decode_json(data)


def _safe(root, relative):
    p = PurePosixPath(relative)
    if not isinstance(relative, str) or not relative or p.is_absolute() or str(p) != relative or '..' in p.parts or '\\' in relative or ':' in relative:
        _fail('Unsafe source evidence relative path')
    path = root / relative
    for item in (path, *path.parents):
        if item == root:
            break
        mode = item.lstat()
        if stat.S_ISLNK(mode.st_mode) or getattr(mode, 'st_file_attributes', 0) & 0x400:
            _fail('Symlink/reparse source evidence')
    if not path.is_file() or not path.resolve().is_relative_to(root):
        _fail('Missing/escaped source evidence file')
    return path


def _tree(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        _fail('Expected actual source evidence directory')
    root = root.resolve(strict=True)
    result = {}
    total = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            _fail('Symlink/reparse source evidence tree')
        if path.is_dir():
            continue
        if not path.is_file():
            _fail('Special source evidence file')
        name = path.relative_to(root).as_posix()
        if name == RECEIPT:
            continue
        _safe(root, name)
        size = path.stat().st_size
        total += size
        if size > 256 * 1024 * 1024 or total > 2 * 1024**3 or len(result) >= 50000:
            _fail('Source evidence exceeds bounded inventory')
        result[name] = {'bytes': size, 'sha256': _sha(path)}
    return root, result


def expected_identity(commit, repository, run_id, run_attempt):
    result = {'commit': commit, 'repository': repository, 'run_id': run_id, 'run_attempt': run_attempt}
    if (not _hex(commit, 40) or not isinstance(repository, str)
            or not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not _positive(run_id) or not _positive(run_attempt)):
        _fail('Missing exact expected source SHA/run/repository/attempt')
    return result


def _path(state, value):
    return (PureWindowsPath if state['identity']['profile'].startswith('windows-') else PurePosixPath)(value)


def _under(state, child, parent):
    a, b = _path(state, child), _path(state, parent)
    return a.is_absolute() and b.is_absolute() and '..' not in a.parts and a.is_relative_to(b)


def _equal_fields(document, expected, label):
    for key, value in expected.items():
        if document.get(key) != value or type(document.get(key)) is not type(value):
            _fail(label + ': ' + key)


def _steps(root, inventory, expected, profile):
    state = _json(_safe(root, 'steps.json'))
    _equal_fields(state, {'schema': 1}, 'Source steps schema')
    identity = state.get('identity', {})
    target = runner.PROFILES[profile]
    _equal_fields(identity, {**expected, 'profile': profile, 'runner_os': target[0],
                            'runner_arch': target[1], 'runner': target[2]}, 'Source CI identity')
    if not re.fullmatch('[A-Za-z0-9_-]+', str(identity.get('job', ''))):
        _fail('Missing logical source job identity')
    hashes = {name: _sha(ROOT / name) for name in contract_files()}
    if state.get('checkout') != {'commit': expected['commit'], 'dirty': False, 'files': hashes}:
        _fail('Source checkout/contract files differ from final checked-out code')
    paths = state.get('paths', {})
    if set(paths) != {'checkout', 'evidence', 'models', 'venv'} or any(not _path(state, value).is_absolute() for value in paths.values()):
        _fail('Missing actual source producer paths')
    if state.get('models') != paths['models']:
        _fail('Source model root identity mismatch')
    environment = state.get('environment', {})
    _equal_fields(environment, {'python': [3, 12], 'user_site_enabled': False, 'venv_created_new': True,
                               'system_site_packages': False, 'loader_overrides': {}}, 'Fresh source environment')
    if (not _hex(environment.get('interpreter_sha256')) or not _hex(environment.get('pyvenv_cfg_sha256'))
            or _path(state, environment.get('prefix', '')) != _path(state, paths['venv'])
            or _path(state, environment.get('base_prefix', '')) == _path(state, paths['venv'])
            or not _under(state, environment.get('interpreter', ''), paths['venv'])):
        _fail('Source interpreter does not belong to the freshly created venv')
    required = {'venv-create', 'venv-probe', 'pip-install', 'pip-check', 'source-default', 'models-verify',
                'vsf-verify', 'gui-smoke', 'cli-version', 'cli-help', 'image-cpu', 'video'}
    if profile == 'macos-ort-cpu-coreml':
        required.add('image-coreml')
    if profile.startswith('windows-'):
        required.add('cuda-environment')
    rows = state.get('steps', [])
    if len(rows) != len(required) or {row.get('name') for row in rows} != required:
        _fail('Missing/duplicate/extra actual source command step')
    previous = 0
    for index, row in enumerate(rows):
        name = row['name']
        if (type(row.get('returncode')) is not int or row['returncode'] != 0 or 'error' in row
                or not _positive(row.get('started_utc_ns')) or not _positive(row.get('ended_utc_ns'))
                or not previous <= row['started_utc_ns'] <= row['ended_utc_ns']):
            _fail('Failed or invalid source command interval')
        previous = row['ended_utc_ns']
        if name == 'venv-create':
            argv = row.get('argv', [])
            if index != 0 or len(argv) != 4 or not _path(state, argv[0]).is_absolute() or argv[1:] != ['-m', 'venv', paths['venv']]:
                _fail('Not an actual fresh venv creation command')
        elif name == 'venv-probe':
            if index != 1 or row.get('argv') != [environment['interpreter'], '-I', '-c', runner.PROBE]:
                _fail('Wrong actual new-interpreter probe command')
        elif row.get('argv') != runner.command(state, _path(state, paths['evidence']), name):
            _fail('Source step command differs from its fixed recipe: ' + name)
        for stream in ('stdout', 'stderr'):
            item = row.get(stream, {})
            if item.get('path') != name + '.' + stream + '.log' or inventory.get(item.get('path')) != {k: item.get(k) for k in ('bytes', 'sha256')}:
                _fail('Source step log does not match its original bytes')
    probe = next(row for row in rows if row['name'] == 'venv-probe')
    if _json(_safe(root, probe['stdout']['path'])) != environment:
        _fail('Environment is not the actual child probe stdout')
    default = next(row for row in rows if row['name'] == 'source-default')
    default_report = _json(_safe(root, 'source-default.json'))
    if default_report != _json(_safe(root, default['stdout']['path'])):
        _fail('Source default evidence differs from actual isolated child stdout')
    _equal_fields(default_report, {'source_default': 'paddle', 'custom_worker_files': [],
                                  'interpreter': environment['interpreter']}, 'Source default without worker')
    return state, {row['name']: row for row in rows}


def _record_context(record, state, window, seen):
    env = state['environment']
    if record.get('interpreter') != env['interpreter'] or record.get('interpreter_sha256') != env['interpreter_sha256']:
        _fail('Inference did not use the recorded source interpreter')
    runtime = record['runtime']
    runtime_key = 'darwin-arm64-cp312' if state['identity']['profile'].startswith('macos') else 'linux-x86_64-cp312'
    pin = _json(ROOT / 'backend/tools/python_coreml_runtime.json')['wheels'][runtime_key]
    if (runtime.get('wheel_filename') != pin['filename']
            or not _under(state, runtime.get('package_root', ''), env['prefix'])
            or not _under(state, runtime.get('binding_path', ''), str(_path(state, runtime.get('package_root', '')) / 'capi'))):
        _fail('Python runtime import did not belong to this platform/venv')
    started, ended = record['started']['utc_ns'], record['ended']['utc_ns']
    if not window['started_utc_ns'] <= started <= ended <= window['ended_utc_ns']:
        _fail('Inference evidence is outside its actual source command interval')
    key = (record['process_pid'], record['engine_id'], record['request_id'], started)
    if key in seen:
        _fail('Source inference request replayed across model/video modes')
    seen.add(key)


def _onnx_image(report, state, provider, window, seen):
    _equal_fields(report, {'schema': 1, 'scope': 'python-source-correctness', 'backend': 'python-coreml',
        'commit': state['identity']['commit'], 'provider': provider, 'case_set': 'all', 'result': 'passed',
        'network_attempts': [], 'subprocess_attempts': [], 'native_worker_used': False,
        'gpu_execution_verified': False, 'internal_coreml_precision_verified': False,
        'final_package_accepted': False, 'models_unchanged': True, 'source_unchanged': True,
        'paddle_imported': False, 'runtime_files_unchanged': True, 'loader_environment_clean': True,
        'confidence_tolerance': frozen.TOLERANCE, 'interpreter': state['environment']['interpreter']}, 'Python image report')
    cases, specs = report.get('cases', []), frozen.case_specs()
    if len(cases) != 168 or {row.get('id') for row in cases} != set(range(168)) or frozen._case_proof(cases) != frozen.CASE_PROOF_SHA256:
        _fail('Python source lacks all canonical 168 model/mode/fixture cases')
    records = []
    for row in cases:
        if type(row.get('id')) is not int or row.get('passed') is not True or any(row.get(k) != v for k, v in specs[row['id']].items()):
            _fail('Python source model case identity differs')
        if row['operation'] == 'ocr':
            verdict = frozen.compare_ocr(row['actual'], row['expected'])
            if not verdict['passed'] or any(row.get(k) != v for k, v in verdict.items()):
                _fail('Python source OCR differs from frozen actual comparison')
        elif not frozen._polygons(row['actual']) or row['actual'] != row['expected']:
            _fail('Python source detector differs from frozen boxes')
        record = row['request_evidence']
        request = record['request']
        if request.get('tier') != row['tier'] or request.get('operation') != row['operation'] or (row['operation'] == 'ocr' and request.get('rec_model') != row['rec_model']):
            _fail('Python source request is not bound to its model/mode case')
        if record.get('result_empty') is not (not (row['actual'][1] if row['operation'] == 'ocr' else row['actual'])):
            _fail('Python empty-result evidence differs from its actual outcome')
        records.append(record)
    covered = validate_python_coreml_records(records, provider=provider)
    if covered != frozen.ALL_MODELS or report.get('covered_models') != sorted(covered):
        _fail('Python source did not actually cover all pinned graphs')
    for row, record in zip(cases, records):
        executed = sorted(s['model'] for s in record['sessions'].values() if s and s['status'] == 'executed')
        if row.get('executed_models') != executed:
            _fail('Python executed-model summary differs from actual request')
        _record_context(record, state, window, seen)
    first = records[0]
    if report.get('source_files') != first['source_files'] or report.get('runtime') != first['runtime']:
        _fail('Python report source/runtime identity differs from actual records')
    if any(record['runtime'] != report['runtime'] for record in records):
        _fail('Python image runtime changed between requests')
    runtime_key = 'darwin-arm64-cp312' if state['identity']['profile'].startswith('macos') else 'linux-x86_64-cp312'
    runtime_pin = _json(ROOT / 'backend/tools/python_coreml_runtime.json')['wheels'][runtime_key]
    if report['runtime'].get('wheel_filename') != runtime_pin['filename']:
        _fail('Python CPU/runtime wheel belongs to a different platform')
    natives = report['runtime']['native_files']
    if report.get('runtime_native_files_before') != natives or report.get('runtime_native_files_after') != natives:
        _fail('Missing independent Python runtime before/after rehash')
    fixed = _json(ROOT / 'native/ocr/model-source-pins.json')
    required = {'model-source-pins.json': frozen.SOURCE_PINS_SHA256, **fixed['config_sha256'],
                **{'models/' + row['name'] + '_infer.onnx': row['onnx_sha256'] for row in fixed['models']}}
    hashes = report.get('model_files', {})
    if set(hashes) != {*required, 'model-manifest.json'} or any(hashes.get(k) != v for k, v in required.items()) or not _hex(hashes.get('model-manifest.json')):
        _fail('Python model/config inventory differs from fixed pins')


def _paddle_image(report, state):
    frozen.validate_report(report, identity=None)
    if report.get('scope') != 'source' or report.get('bundled_python_verified') is not False or any(report.get(k) is not None for k in ('commit', 'version', 'build_profile')):
        _fail('Paddle source report must not impersonate a final bundle')
    if report.get('python') != state['environment']['interpreter'] or _path(state, report.get('app_root', '')) != _path(state, state['paths']['checkout']):
        _fail('Paddle source interpreter/checkout mismatch')
    if (_path(state, report.get('model_root', '')) != _path(state, state['models'])
            or report.get('device', {}).get('compiled_with_cuda') is not True
            or any(report.get('runtime_versions', {}).get(k) != v for k, v in {'paddle': '3.3.1', 'paddleocr': '3.7.0'}.items())):
        _fail('Paddle image gate did not use this CUDA source environment/models')
    modules = report.get('production_modules', {})
    expected = {_path(state, state['paths']['checkout']) / (name.replace('.', '/') + '.py'): _sha(ROOT / (name.replace('.', '/') + '.py')) for name in frozen.PRODUCTION_MODULES}
    actual = {_path(state, path): digest for path, digest in modules.items()}
    if actual != expected or len(actual) != len(modules):
        _fail('Paddle production module origins/hashes differ from final source')
    fixed = _json(ROOT / 'native/ocr/model-source-pins.json')
    from scripts.paddle_package_models import _model_path
    required = {_model_path(row['name']) + '/' + name: digest for row in fixed['models'] for name, digest in row['files'].items()}
    files = report.get('model_files', {})
    if set(files) != set(required) or any(files[k].get('sha256') != h or not _positive(files[k].get('bytes')) for k, h in required.items()):
        _fail('Paddle original model file inventory differs from fixed pins')


def _srt(path):
    with _opened(path) as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        _fail('Oversized SRT evidence')
    data = data.decode('utf-8-sig').replace('\r\n', '\n').strip()
    intervals, texts = [], []
    for block in re.split(r'\n\s*\n', data):
        lines = block.splitlines()
        if len(lines) < 3 or not re.fullmatch('[0-9]+', lines[0]):
            _fail('Malformed actual source SRT')
        match = re.fullmatch(r'(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})', lines[1])
        if not match:
            _fail('Malformed source SRT timing')
        fields = list(map(int, match.groups()))
        a, b = [(h * 3600 + m * 60 + s) * 1000 + ms for h, m, s, ms in (fields[:4], fields[4:])]
        if not 0 <= a < b <= 7100 or (intervals and a < intervals[-1][1]):
            _fail('Invalid or overlapping actual source subtitle intervals')
        intervals.append([a, b]); texts.append(' '.join(' '.join(lines[2:]).upper().split()))
    for text, a, b in (('HELLO WORLD', 1000, 3000), ('SECOND LINE', 4000, 6000)):
        if not any(text in actual and abs(t[0] - a) <= 600 and abs(t[1] - b) <= 600 for actual, t in zip(texts, intervals)):
            _fail('Actual SRT misses expected text/timing')
    return {'recognized': ['HELLO WORLD', 'SECOND LINE'], 'subtitle_count': len(intervals), 'intervals_ms': intervals}


def _video(root, inventory, state, window, seen):
    report = _json(_safe(root, 'video/report.json'))
    profile = state['identity']['profile']
    windows = profile.startswith('windows-')
    providers = ('cpu', 'coreml') if profile == 'macos-ort-cpu-coreml' else ('cpu',)
    if windows:
        _equal_fields(report, {'result': 'passed', 'modes_requested': list(MODES), 'cpu_forced': True,
                              'requested_provider': 'cpu', 'gpu_inference_verified': False}, 'Paddle source video')
    else:
        _equal_fields(report, {'schema': 1, 'scope': 'python-source-video-acceptance', 'backend': 'python-coreml',
            'result': 'passed', 'providers': list(providers), 'modes': list(MODES),
            'gpu_execution_verified': False, 'final_package_accepted': False, 'timing_is_benchmark': False,
            'commit': state['identity']['commit'], 'interpreter': state['environment']['interpreter'],
            'interpreter_sha256': state['environment']['interpreter_sha256']}, 'Python source video')
    rows = report.get('cases', [])
    expected = [(mode, provider) for mode in MODES for provider in providers]
    if len(rows) != len(expected):
        _fail('Source video must execute every required provider/mode')
    peer = {}
    for row, (mode, provider) in zip(rows, expected):
        folder = 'video/' + (mode if windows else mode + '-' + provider)
        if row != _json(_safe(root, folder + '/report.json')):
            _fail('Source video aggregate differs from original per-mode report')
        _equal_fields(row, {'result': 'passed', 'mode': mode, 'cpu_forced': provider == 'cpu',
                           'requested_provider' if windows else 'provider': provider,
                           'gpu_inference_verified' if windows else 'gpu_execution_verified': False}, 'Source video mode')
        if not windows and row.get('timing_is_benchmark') is not False:
            _fail('Source correctness timing is not a benchmark')
        command = row.get('command', [])
        if (not isinstance(command, list) or command[:3] != [state['environment']['interpreter'], '-m', 'backend.cli']
                or command.count('--mode') != 1 or command[command.index('--mode') + 1] != mode
                or command.count('--cpu') != (1 if provider == 'cpu' else 0)):
            _fail('Source CLI did not use explicit provider/mode in the same venv')
        subtitle = _safe(root, folder + '/recognized.srt')
        values = _srt(subtitle)
        if any(row.get(k) != v for k, v in values.items()) or row.get('srt_sha256') != _sha(subtitle):
            _fail('Source SRT bytes differ from their report')
        video_name = folder + '/input video.mp4' if windows else 'video/same input video.mp4'
        remote = _path(state, state['paths']['evidence'])
        expected_command = [state['environment']['interpreter'], '-m', 'backend.cli', str(remote / video_name),
            '--output', str(remote / folder / 'recognized.srt'), '--language', 'en', '--mode', mode,
            *(['--cpu'] if provider == 'cpu' else []), '--threads', '2', '--area', '0.6,1,0,1',
            '--overwrite', '--no-word-segmentation']
        if command != expected_command:
            _fail('Inner source CLI argv does not bind the actual input/output and production options')
        if row.get('input_video_sha256') != _sha(_safe(root, video_name)):
            _fail('Source video hash differs from actual input')
        if not windows:
            if report.get('input_video_sha256') != row['input_video_sha256']:
                _fail('Python providers did not receive identical video bytes')
            requests = row.get('python_requests', [])
            directory = folder + '/python-requests/'
            names = {key.removeprefix(directory) for key in inventory if key.startswith(directory)}
            if not requests or len(requests) != len(names) or {r.get('path') for r in requests} != names:
                _fail('Missing/duplicated/unindexed Python video request evidence')
            documents = []
            for item in requests:
                name = item['path']
                if not re.fullmatch(r'python-ocr-[1-9][0-9]*-[1-9][0-9]*-[1-9][0-9]*\.json', name) or inventory.get(directory + name) != {k: item.get(k) for k in ('bytes', 'sha256')}:
                    _fail('Python source request index differs from raw evidence')
                record = _json(_safe(root, directory + name))
                if name != 'python-ocr-{}-{}-{}.json'.format(record.get('process_pid'), record.get('engine_id'), record.get('request_id')):
                    _fail('Python request filename/process identity mismatch')
                documents.append(record)
            executed = validate_python_coreml_records(documents, provider=provider)
            tier = 'medium' if mode == 'accurate' else 'small'
            if executed != {f'PP-OCRv6_{tier}_det', f'PP-OCRv6_{tier}_rec'} or row.get('executed_models') != sorted(executed):
                _fail('Video did not execute the required real model pair')
            for document in documents:
                _record_context(document, state, window, seen)
        value = (values, row['srt_sha256'])
        if mode in peer and peer[mode] != value:
            _fail('Python CPU/CoreML full SRT output differs')
        peer[mode] = value


def _installation(root, state):
    profile = state['identity']['profile']
    pip = _json(_safe(root, 'pip-install.json'))
    if profile.startswith('windows-'):
        cuda = profile.removeprefix('windows-cuda')
        pinned, wheels = cuda_checker.load_profile(cuda)
        verified = cuda_checker.validate_install_report(pip, wheels)
        report = _json(_safe(root, 'environment.json'))
        _equal_fields(report, {'schema': 1, 'scope': 'python-cuda-source-environment', 'cuda': cuda,
            'result': 'passed', 'gpu_inference_verified': False, 'native_worker_used': False,
            'ocr_cases_verified': False, 'final_package_accepted': False,
            'source_wheels': verified, 'reviewed_wheel_provenance': pinned,
            'pip_report_sha256': _sha(_safe(root, 'pip-install.json'))}, 'CUDA source installation')
        identity = report.get('runtime', {})
        _equal_fields(identity, {'platform': 'win32', 'implementation': 'CPython', 'python': [3, 12],
            'user_site_enabled': False, 'paddle_version': '3.3.1', 'compiled_with_cuda': True,
            'compiled_cuda_version': cuda, 'actual_device': 'cpu'}, 'Actual CUDA build on explicit CPU')
        if identity.get('architecture') not in ('amd64', 'x86_64'):
            _fail('CUDA source must run on x64 Windows')
        for key in ('interpreter', 'prefix', 'base_prefix'):
            if _path(state, identity.get(key, '')) != _path(state, state['environment'][key]):
                _fail('CUDA environment did not use this job venv')
        if not _under(state, identity.get('paddle_origin', ''), identity['prefix']):
            _fail('Paddle imported outside the ordinary-pip source environment')
        packages = identity.get('distributions', {})
        expected = {'paddlepaddle-gpu': '3.3.1', 'paddleocr': '3.7.0', 'paddlex': '3.7.2'}
        if cuda == '12.6':
            expected['nvidia-nvjitlink-cu12'] = '12.6.85'
        if 'paddlepaddle' in packages or any(packages.get(k) != v for k, v in expected.items()):
            _fail('Mixed or wrong actual source CUDA packages')
        qt = [packages.get(k) for k in ('pyside6', 'pyside6-essentials', 'pyside6-addons', 'shiboken6')]
        if not all(qt) or len(set(qt)) != 1:
            _fail('Source pip must keep the genuine matching full Qt dependency set')
    else:
        key = 'darwin-arm64-cp312' if profile.startswith('macos') else 'linux-x86_64-cp312'
        pin = _json(ROOT / 'backend/tools/python_coreml_runtime.json')['wheels'][key]
        installed = [cuda_checker.canonical(row.get('metadata', {}).get('name', '')) for row in pip.get('install', [])]
        if len(installed) != len(set(installed)) or {name for name in installed if name.startswith('onnxruntime')} != {'onnxruntime'}:
            _fail('Mixed or duplicate source ONNX Runtime installation')
        rows = [row for row in pip.get('install', []) if cuda_checker.canonical(row.get('metadata', {}).get('name', '')) == 'onnxruntime']
        if len(rows) != 1:
            _fail('Pip report must contain one actual Python ORT install')
        row = rows[0]; download = row.get('download_info', {})
        urls = re.findall(r'onnxruntime @ (https://\S+)', (ROOT / 'requirements-coreml-inference.txt').read_text())
        approved = next(url.split('#')[0] for url in urls if '/' + pin['filename'] + '#' in url)
        if (row.get('metadata', {}).get('version') != '1.31.0' or download.get('url') != approved
                or download.get('archive_info', {}).get('hashes', {}).get('sha256') != pin['sha256']
                or row.get('is_direct') is not True or row.get('requested') is not True):
            _fail('Python ORT source pip wheel differs from its whole-wheel pin')
    install = _json(_safe(root, 'vsf-install.json'))
    lock = _json(ROOT / 'backend/tools/videosubfinder_release.json')
    platform, arch = ('windows', 'x64') if profile.startswith('windows') else ('macos', 'arm64') if profile.startswith('macos') else ('linux', 'x64')
    from backend.tools.videosubfinder import release_asset, VSF_RELEASE_BASE
    asset = release_asset(platform, arch)
    _equal_fields(install, {'version': '6.10.7', 'platform': platform, 'arch': arch, 'cuda': False,
        'asset': asset, 'source': VSF_RELEASE_BASE + '/v6.10.7/' + asset, 'release_locked': True,
        'sha256sums_sha256': lock['sha256sums_sha256']}, 'Pinned VSF source installation')
    if not _hex(install.get('sha256')):
        _fail('Missing actual VSF archive identity')


def checks(profile):
    result = {'fresh_venv': 'passed', 'ordinary_pip_install': 'passed', 'pip_check': 'passed',
              'verified_models': 'passed', 'vsf_6_10_7': 'passed', 'gui_startup': 'passed',
              'cli_entrypoints': 'passed', 'cpu_168_three_modes': 'passed', 'cpu_three_videos': 'passed'}
    if profile == 'macos-ort-cpu-coreml':
        result.update(coreml_168_three_modes='passed', coreml_three_videos='passed', cpu_coreml_same_srt='passed')
    if profile.startswith('windows-'):
        result['cuda_build_explicit_cpu'] = 'passed'
    return result


def validate_evidence(evidence, *, expected, profile):
    if profile not in PROFILES:
        _fail('Unexpected source profile')
    root, inventory = _tree(evidence)
    state, steps = _steps(root, inventory, expected, profile)
    _installation(root, state)
    seen = set()
    cpu = _json(_safe(root, 'cpu-all.json'))
    if profile.startswith('windows-'):
        _paddle_image(cpu, state)
    else:
        _onnx_image(cpu, state, 'cpu', steps['image-cpu'], seen)
    if profile == 'macos-ort-cpu-coreml':
        _onnx_image(_json(_safe(root, 'coreml-all.json')), state, 'coreml', steps['image-coreml'], seen)
    _video(root, inventory, state, steps['video'], seen)
    if {name: _sha(ROOT / name) for name in contract_files()} != state['checkout']['files']:
        _fail('Source contract changed during acceptance')
    if _tree(root)[1] != inventory:
        _fail('Source evidence changed during acceptance')
    return {'schema': 1, 'scope': SCOPE, 'result': 'passed', 'identity': state['identity'],
            'checkout_files': state['checkout']['files'], 'checks': checks(profile), 'evidence': inventory,
            'evidence_sha256': hashlib.sha256(_canonical(inventory)).hexdigest(),
            'gpu_inference_verified': False, 'final_package_accepted': False,
            'coreml_internal_precision_verified': False}


def _write(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def produce(evidence, *, expected):
    if Path(evidence).is_symlink() or not Path(evidence).is_dir():
        _fail('Unsafe source evidence root')
    root = Path(evidence).resolve(strict=True)
    output = root / RECEIPT
    if output.is_symlink():
        _fail('Unsafe source receipt path')
    output.unlink(missing_ok=True)  # A failed verification cannot retain old success.
    profile = _json(_safe(root, 'steps.json'))['identity']['profile']
    receipt = validate_evidence(root, expected=expected, profile=profile)
    _write(output, receipt)
    return receipt


def aggregate(evidence_root, *, expected, output):
    root = Path(evidence_root)
    if root.is_symlink() or not root.is_dir() or {p.name for p in root.iterdir()} != set(PROFILES):
        _fail('Source aggregation requires exactly four profile evidence directories')
    root = root.resolve(strict=True)
    profiles = {}
    for profile in PROFILES:
        path = root / profile
        if path.is_symlink():
            _fail('Unsafe profile evidence directory')
        actual = validate_evidence(path, expected=expected, profile=profile)
        receipt_path = _safe(path, RECEIPT)
        if _json(receipt_path) != actual:
            _fail('Source receipt differs from independently revalidated raw evidence')
        profiles[profile] = {'receipt_sha256': _sha(receipt_path), 'evidence_sha256': actual['evidence_sha256'],
                             'checks': actual['checks'], 'identity': actual['identity']}
    result = {'schema': 1, 'scope': SCOPE, 'kind': 'complete-python-source-set', 'result': 'passed',
              'identity': expected, 'profiles': profiles, 'raw_evidence_revalidated': True,
              'gpu_inference_verified': False, 'final_package_accepted': False,
              'coreml_internal_precision_verified': False,
              'contract_files': {name: _sha(ROOT / name) for name in contract_files()}}
    _write(output, result)
    return result


def verify_aggregate(path, *, expected, sha256):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not _hex(sha256):
        _fail('Unsafe aggregate or missing external SHA256')
    with _opened(path) as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON or hashlib.sha256(data).hexdigest() != sha256:
        _fail('Aggregate does not match the externally expected SHA256')
    result = _decode_json(data)
    _equal_fields(result, {'schema': 1, 'scope': SCOPE, 'kind': 'complete-python-source-set', 'result': 'passed',
                          'identity': expected, 'raw_evidence_revalidated': True,
                          'gpu_inference_verified': False, 'final_package_accepted': False,
                          'coreml_internal_precision_verified': False,
                          'contract_files': {name: _sha(ROOT / name) for name in contract_files()}}, 'Source aggregate identity')
    if set(result.get('profiles', {})) != set(PROFILES):
        _fail('Incomplete source aggregate profile matrix')
    for profile, record in result['profiles'].items():
        target = runner.PROFILES[profile]
        _equal_fields(record.get('identity', {}), {**expected, 'profile': profile, 'runner_os': target[0],
            'runner_arch': target[1], 'runner': target[2]}, 'Source aggregate profile identity')
        if record.get('checks') != checks(profile) or not _hex(record.get('receipt_sha256')) or not _hex(record.get('evidence_sha256')):
            _fail('Missing source aggregate acceptance/checksum binding')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('produce', 'aggregate', 'verify'))
    parser.add_argument('--evidence', type=Path)
    parser.add_argument('--evidence-root', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--aggregate', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--commit', default=os.environ.get('GITHUB_SHA'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--run-id', type=int, default=os.environ.get('GITHUB_RUN_ID'))
    parser.add_argument('--run-attempt', type=int, default=os.environ.get('GITHUB_RUN_ATTEMPT'))
    args = parser.parse_args(argv)
    expected = expected_identity(args.commit, args.repository, args.run_id, args.run_attempt)
    if args.operation == 'produce' and args.evidence:
        result = produce(args.evidence, expected=expected)
    elif args.operation == 'aggregate' and args.evidence_root and args.output:
        result = aggregate(args.evidence_root, expected=expected, output=args.output)
    elif args.operation == 'verify' and args.aggregate and args.sha256:
        result = verify_aggregate(args.aggregate, expected=expected, sha256=args.sha256)
    else:
        parser.error('Missing operation-specific paths or external aggregate SHA256')
    print(json.dumps({'result': result['result'], 'scope': result['scope'], 'identity': result['identity']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
