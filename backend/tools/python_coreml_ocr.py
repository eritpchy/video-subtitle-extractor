"""Full Python OCR using the official Python ORT wheel, never a VSE C++ worker.

The existing Python DB/crop/CTC implementation is shared with DirectML, while
runtime, provider, shape-session and evidence policies here are independent.
CPU is an explicit option. CoreML EP execution is not physical GPU proof.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time

from backend.tools.directml_ocr import DirectMlOcr, validate_model_assets, MODEL_SOURCE_PINS_SHA256

ORT_VERSION = '1.31.0'
COREML = 'CoreMLExecutionProvider'
CPU = 'CPUExecutionProvider'
PROVIDER_OPTIONS = {'ModelFormat': 'MLProgram', 'MLComputeUnits': 'CPUAndGPU',
                    'AllowLowPrecisionAccumulationOnGPU': '0',
                    'RequireStaticInputShapes': '1', 'EnableOnSubgraphs': '0'}
_SESSION_IDS = itertools.count(1)
_ENGINE_IDS = itertools.count(1)
_RUNTIME = None


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _load_runtime():
    """No import-time telemetry race, conflicting ORT package, or guessed SDK."""
    global _RUNTIME
    if 'onnxruntime' in sys.modules and os.environ.get('ORT_DISABLE_TELEMETRY') != '1':
        raise RuntimeError('Python ORT was imported before telemetry was disabled; start a fresh process')
    os.environ['ORT_DISABLE_TELEMETRY'] = '1'
    os.environ.pop('MTL_CAPTURE_ENABLED', None)
    if _RUNTIME is not None:
        return _RUNTIME
    distributions = {}
    for dist in importlib.metadata.distributions():
        name = (dist.metadata.get('Name') or '').lower().replace('_', '-')
        if name.startswith('onnxruntime'):
            if name in distributions:
                raise RuntimeError('Duplicate Python ONNX Runtime distributions')
            distributions[name] = dist.version
    if distributions != {'onnxruntime': ORT_VERSION}:
        raise RuntimeError('Python CoreML requires only onnxruntime==1.31.0; use a separate environment from DirectML/GPU ORT')
    import onnxruntime as ort
    if ort.__version__ != ORT_VERSION or ort.get_version_string() != ORT_VERSION:
        raise RuntimeError('Python/native ONNX Runtime version differs from the source pin')
    ort.disable_telemetry_events()  # Second layer; environment was set before import.
    root = Path(ort.__file__).resolve().parent
    pins = json.loads(Path(__file__).with_name('python_coreml_runtime.json').read_bytes())
    key = f'{sys.platform}-{platform.machine().lower()}-cp{sys.version_info.major}{sys.version_info.minor}'
    pin = pins['wheels'].get(key)
    if pin is None:
        raise RuntimeError('No verified Python CoreML runtime wheel for ' + key)
    actual = {}
    for relative, expected in pin['native_files'].items():
        path = root / relative
        if path.is_symlink() or not path.is_file() or _sha(path) != expected:
            raise RuntimeError('Python ORT native module differs from the exact official wheel: ' + relative)
        actual[relative] = expected
    # Reject unpinned replacement/additional runtime binaries, not just a name.
    discovered = {path.relative_to(root).as_posix() for path in root.rglob('*')
                  if path.is_file() and (path.suffix in ('.so', '.dylib', '.pyd', '.dll') or '.so.' in path.name)}
    if discovered != set(actual):
        raise RuntimeError('Python ORT contains unexpected/missing native modules')
    import onnxruntime.capi.onnxruntime_pybind11_state as binding
    if Path(binding.__file__).resolve().parent != root / 'capi':
        raise RuntimeError('Python ORT loaded its binding outside the pinned package')
    identity = {'version': ORT_VERSION, 'package_root': str(root),
                'wheel_filename': pin['filename'], 'wheel_sha256': pin['sha256'],
                'native_files': actual, 'binding_path': str(Path(binding.__file__).resolve()),
                'telemetry_disabled_before_import': True,
                'available_providers': ort.get_available_providers()}
    _RUNTIME = ort, identity
    return _RUNTIME


def python_coreml_capabilities():
    if sys.platform != 'darwin' or platform.machine().lower() != 'arm64':
        raise RuntimeError('Python CoreML requires macOS arm64; choose explicit CPU on this host')
    ort, identity = _load_runtime()
    if COREML not in ort.get_available_providers():
        raise RuntimeError('Python CoreML provider unavailable; automatic CPU fallback is disabled')
    return dict(identity, requested_provider='coreml', compute_units='CPUAndGPU',
                model_format='MLProgram', ane_allowed=False, gpu_execution_verified=False)


def _shape_bindings(role, shape, batch_size):
    if (len(shape) != 4 or any(type(value) is not int or value <= 0 for value in shape)
            or math.prod(shape) > 64000000 or shape[1] != 3):
        raise ValueError('Invalid exact Python OCR tensor shape')
    n, _, h, w = shape
    if role == 'det':
        if n != 1 or min(h, w) < 32 or max(h, w) > 4000 or h % 32 or w % 32:
            raise ValueError('Python detector shape differs from unchanged preprocessing')
        return {'DynamicDimension.0': n, 'DynamicDimension.1': h, 'DynamicDimension.2': w}
    if role != 'rec' or not 1 <= n <= batch_size <= 100 or h != 48 or w > 3200:
        raise ValueError('Python recognizer shape differs from unchanged preprocessing')
    return {'DynamicDimension.0': n, 'DynamicDimension.1': w}


def _clock():
    return {'process_pid': os.getpid(), 'utc_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns()}


def _profile_counts(events):
    if not isinstance(events, list) or len(events) > 200000:
        raise RuntimeError('Invalid or oversized Python CoreML profile')
    counts = {COREML: 0, CPU: 0}
    for event in events:
        if not isinstance(event, dict) or event.get('cat') != 'Node' or not str(event.get('name', '')).endswith('_kernel_time'):
            continue
        provider = event.get('args', {}).get('provider')
        if provider is None:
            continue
        if provider not in counts:
            raise RuntimeError('Unexpected provider in Python CoreML execution profile')
        counts[provider] += 1
    if not counts[COREML]:
        raise RuntimeError('No CoreML kernel executed; all-CPU inference labeled CoreML is refused')
    return counts


class _ShapeSession:
    def __init__(self, path, role, provider, shape, batch_size, threads):
        ort, _ = _load_runtime()
        self.provider, self.shape = provider, tuple(shape)
        self.overrides = _shape_bindings(role, list(shape), batch_size)
        self.session_id = next(_SESSION_IDS)
        self.runs = self.request_runs = 0
        self.profile = None
        self.first_evidence = None
        self.session = None
        requested = COREML if provider == 'coreml' else CPU
        if requested not in ort.get_available_providers():
            raise RuntimeError(requested + ' unavailable; no automatic CPU fallback')
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        for name, value in self.overrides.items():
            options.add_free_dimension_override_by_name(name, value)
        if provider == 'coreml':
            self.profile = tempfile.TemporaryDirectory(prefix='vse-python-coreml-profile-')
            options.enable_profiling = True
            options.profile_file_prefix = str(Path(self.profile.name) / 'execution')
        providers = [(COREML, dict(PROVIDER_OPTIONS))] if provider == 'coreml' else [CPU]
        try:
            self.session = ort.InferenceSession(str(path), sess_options=options, providers=providers)
            self.session.disable_fallback()
            if requested not in self.session.get_providers():
                raise RuntimeError('Python ORT constructor silently fell back; requested EP was not retained')
            inputs = self.session.get_inputs()
            if len(inputs) != 1 or inputs[0].name != 'x' or inputs[0].type != 'tensor(float)' or inputs[0].shape != list(shape):
                raise RuntimeError('Python ORT did not bind the exact pinned FLOAT32 input shape')
        except BaseException:
            self.close()
            raise

    def run(self, value):
        import numpy as np
        if self.session is None or value.dtype != np.float32 or tuple(value.shape) != self.shape:
            raise RuntimeError('Python OCR input does not match its exact compiled shape')
        started = _clock()
        try:
            result = self.session.run(None, {'x': value})[0]
            ended = _clock()
            if not np.isfinite(result).all():
                raise RuntimeError('Python ORT produced nonfinite output')
            if self.first_evidence is None:
                counts = {COREML: 0, CPU: 0}
                if self.provider == 'coreml':
                    path = Path(self.session.end_profiling())
                    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(Path(self.profile.name).resolve())
                            or path.stat().st_size > 64 * 1024 * 1024):
                        raise RuntimeError('Unsafe or oversized Python CoreML profile')
                    data = path.read_bytes()
                    counts = _profile_counts(json.loads(data))
                    profile_sha = hashlib.sha256(data).hexdigest()
                    self.profile.cleanup(); self.profile = None
                else:
                    profile_sha = None
                self.first_evidence = {'profile_started': started, 'profile_ended': ended,
                    'source': 'ort_first_run_profile' if self.provider == 'coreml' else 'explicit_cpu_session',
                    'profile_sha256': profile_sha, 'kernel_events': counts,
                    'coreml_ep_executed': counts[COREML] > 0,
                    'cpu_partition_observed': counts[CPU] > 0,
                    'gpu_execution_verified': False}
            self.runs += 1; self.request_runs += 1
            return result
        except BaseException:
            self.close()
            raise

    def evidence(self):
        return dict(self.first_evidence, shape_session_id=self.session_id, input_shape=list(self.shape),
                    free_dimension_overrides=dict(self.overrides), require_static_input_shapes=True,
                    request_run_count=self.request_runs, session_run_count=self.runs,
                    evidence_reused=self.runs > self.request_runs)

    def close(self):
        self.session = None
        if self.profile is not None:
            self.profile.cleanup(); self.profile = None


class _ShapeDispatcher:
    def __init__(self, path, role, provider, batch_size, threads):
        self.path, self.role, self.provider = path, role, provider
        self.batch_size, self.threads = batch_size, threads
        self.capacity = 2 if role == 'det' else 4
        self.cache = OrderedDict()
        self.snapshots = {}
        self.runs = self.request_runs = 0

    def begin(self):
        self.snapshots = {}; self.request_runs = 0
        for child in self.cache.values():
            child.request_runs = 0

    def run(self, value):
        shape = tuple(int(v) for v in value.shape)
        _shape_bindings(self.role, list(shape), self.batch_size)
        child = self.cache.pop(shape, None)
        if child is None:
            if len(self.cache) == self.capacity:
                _, evicted = self.cache.popitem(last=False); evicted.close()
            child = _ShapeSession(self.path, self.role, self.provider, shape, self.batch_size, self.threads)
        self.cache[shape] = child
        output = child.run(value)
        self.runs += 1; self.request_runs += 1
        self.snapshots[child.session_id] = child.evidence()
        return output

    def evidence(self):
        return {'model': self.path.name.removesuffix('_infer.onnx'),
                'status': 'executed' if self.request_runs else 'not_run',
                'capture': 'per_shape_first_successful_run',
                'request_run_count': self.request_runs, 'session_run_count': self.runs,
                'cache_capacity': self.capacity, 'cached_shape_session_ids': [c.session_id for c in self.cache.values()],
                'shape_sessions': list(self.snapshots.values()),
                'gpu_execution_verified': False}

    def close(self):
        for child in self.cache.values():
            child.close()
        self.cache.clear()


class PythonCoreMlOcr(DirectMlOcr):
    """Reuse reviewed Python preprocessing, not DirectML's runtime/session policy."""
    def __init__(self, model_config, batch_number=6, *, stop_event=None, root=None, detection_only=False):
        self.provider = 'coreml' if model_config.hardware_accelerator.is_enabled else 'cpu'
        if self.provider == 'coreml':
            python_coreml_capabilities()
        _, self.runtime_identity = _load_runtime()
        self.batch_number = int(batch_number)
        if not 1 <= self.batch_number <= 100:
            raise ValueError('Python OCR batch size must be in 1..100')
        self.threads = int(os.environ.get('VSE_OCR_CPU_THREADS', min(4, os.cpu_count() or 1)))
        if not 1 <= self.threads <= 128:
            raise ValueError('Python OCR CPU threads must be in 1..128')
        value = root or os.environ.get('VSE_PYTHON_COREML_MODEL_ROOT')
        if not value:
            raise ValueError('Set VSE_PYTHON_COREML_MODEL_ROOT to all 15 verified ONNX models; no download occurs during inference')
        self.root = Path(value).resolve(strict=True)
        verified = validate_model_assets(self.root)  # All 15, not merely the selected language.
        self.model_hashes = verified['hashes']
        self.det_name, self.rec_name = model_config.DET_MODEL_NAME, model_config.REC_MODEL_NAME
        self.det_config = verified['configs'][self.det_name]
        self.rec_config = None if detection_only else verified['configs'][self.rec_name]
        self.detection_only, self.stop_event = detection_only, stop_event
        self.det_session = self.rec_session = None
        self._lock, self._closed = threading.RLock(), False
        self._request_id = 0
        self._engine_id = next(_ENGINE_IDS)
        self.source_identity = _source_identity()
        self.interpreter_sha256 = _sha(sys.executable)
        self.evidence_directory = os.environ.get("VSE_PYTHON_COREML_EVIDENCE_DIR")
        self.tier = model_config.TIER
        self.last_request_evidence = None

    def _session(self, role):
        name = role + '_session'
        if getattr(self, name) is None:
            setattr(self, name, _ShapeDispatcher(self.root / 'models' / f'{getattr(self, role + "_name")}_infer.onnx',
                                                role, self.provider, self.batch_number, self.threads))
        return getattr(self, name)

    @property
    def execution_evidence(self):
        return {role: value.evidence() if (value := getattr(self, role + '_session')) else None
                for role in ('det', 'rec')}

    def _predict(self, image):
        import cv2
        with self._lock:
            self.last_request_evidence = None
            for session in (self.det_session, self.rec_session):
                if session is not None:
                    session.begin()
            self._request_id += 1
            started = _clock()
            try:
                result = super()._predict(image)
                encoded_ok, encoded = cv2.imencode('.png', image)
                if not encoded_ok:
                    raise RuntimeError('Cannot record source input pixel identity')
                self.last_request_evidence = {'schema': 1, 'scope': 'python-source-inference',
                    'backend': 'python-coreml', 'provider': self.provider, 'request_id': self._request_id,
                    'engine_id': self._engine_id, 'interpreter_sha256': self.interpreter_sha256,
                    'source_files': self.source_identity,
                    'request': {'tier': self.tier, 'rec_model': self.rec_name, 'batch_size': self.batch_number,
                                'cpu_threads': self.threads, 'operation': 'detect' if self.detection_only else 'ocr'},
                    'result_empty': not result.get('dt_polys' if self.detection_only else 'rec_texts'),
                    'model_sha256': {name: self.model_hashes[f'models/{name}_infer.onnx']
                                     for name in ([self.det_name] if self.detection_only else [self.det_name, self.rec_name])},
                    'process_pid': os.getpid(), 'interpreter': sys.executable,
                    'runtime': self.runtime_identity, 'started': started, 'ended': _clock(),
                    'input_encoded_png_sha256': hashlib.sha256(encoded.tobytes()).hexdigest(),
                    'input_encoded_png_bytes': encoded.nbytes,
                    'input_identity_scope': 'Python input pixels encoded for audit; no IPC/worker is used',
                    'sessions': self.execution_evidence, 'gpu_execution_verified': False,
                    'internal_coreml_precision_verified': False}
                validate_python_coreml_record(self.last_request_evidence, provider=self.provider)
                if self.evidence_directory is not None:
                    _emit_source_record(self.last_request_evidence, self.evidence_directory)
                return result
            except BaseException:
                self.last_request_evidence = None
                self.close()
                raise


def _source_identity():
    root = Path(__file__).resolve().parents[2]
    names = ('backend/tools/python_coreml_ocr.py', 'backend/tools/python_coreml_runtime.json',
             'backend/tools/directml_ocr.py', 'backend/tools/ocr.py', 'backend/tools/subtitle_detect.py',
             'backend/tools/paddle_model_config.py', 'backend/tools/hardware_accelerator.py')
    return {name: _sha(root / name) for name in names}


def _emit_source_record(record, directory):
    """Explicit diagnostic-only POSIX creation; never use an unsafe fallback.

    This backend has reviewed runtime wheels only for Linux CPU and macOS
    CPU/CoreML. Windows source inference uses Paddle or DirectML instead.
    Reject unsupported directory-relative I/O before touching the destination;
    an AttributeError or pathname-only write is not a safe portability policy.
    """
    import stat
    if (os.name != 'posix'
            or any(type(getattr(os, name, None)) is not int or not getattr(os, name)
                   for name in ('O_DIRECTORY', 'O_NOFOLLOW'))
            or os.open not in getattr(os, 'supports_dir_fd', ())):
        raise RuntimeError('Python source evidence requires secure POSIX directory-relative I/O; '
                           'no unsafe platform fallback is permitted')
    path = Path(directory).expanduser().absolute()
    if path.is_symlink() or '..' in path.parts or str(path).startswith('\\\\'):
        raise ValueError('Unsafe Python source evidence directory')
    path = path.resolve(strict=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('Python source evidence directory must already exist')
    for key in ('process_pid', 'engine_id', 'request_id'):
        if type(record.get(key)) is not int or not 0 < record[key] < 2**63:
            raise ValueError('Invalid source evidence filename identity')
    filename = f"python-ocr-{record['process_pid']}-{record['engine_id']}-{record['request_id']}.json"
    data = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('Python source evidence exceeds 16 MiB')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('Python source evidence directory changed')
        target = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(target, 'wb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
    finally:
        os.close(fd)
    return path / filename


def _canonical_evidence(record):
    """Use the shared pure-Python shape validator, never start a native worker.

    The transient structure is an internal validation view only. The persisted
    source record retains Python process/interpreter identity, not worker claims.
    """
    def window(started, ended):
        return {'worker_pid': record['process_pid'], 'utc_start_ns': started['utc_ns'],
                'utc_end_ns': ended['utc_ns'], 'monotonic_start_ns': started['monotonic_ns'],
                'monotonic_end_ns': ended['monotonic_ns']}
    provider = record['provider']
    sessions = {}
    for role, aggregate in record['sessions'].items():
        if aggregate is None:
            sessions[role] = None
            continue
        children = []
        for row in aggregate['shape_sessions']:
            counts = row['kernel_events']
            observed = [name for name in (COREML, CPU) if counts[name]] if provider == 'coreml' else [CPU]
            children.append({'model': aggregate['model'], 'status': 'executed',
                'source': row['source'], 'capture': 'first_successful_run',
                'shape_session_id': row['shape_session_id'], 'input_shape': row['input_shape'],
                'free_dimension_overrides': row['free_dimension_overrides'], 'require_static_input_shapes': True,
                'request_run_count': row['request_run_count'], 'session_run_count': row['session_run_count'],
                'evidence_reused': row['evidence_reused'], 'coreml_kernel_events': counts[COREML],
                'cpu_kernel_events': counts[CPU], 'coreml_ep_executed': counts[COREML] > 0,
                'cpu_ep_executed': counts[CPU] > 0 if provider == 'coreml' else True,
                'observed_providers': observed, 'gpu_execution_verified': False,
                'profile_window': window(row['profile_started'], row['profile_ended'])})
        coreml_count = sum(row['coreml_kernel_events'] for row in children)
        cpu_count = sum(row['cpu_kernel_events'] for row in children)
        sessions[role] = {**aggregate, 'source': 'ort_first_run_profile' if provider == 'coreml' else 'explicit_cpu_session',
            'capture': 'per_shape_first_successful_run' if provider == 'coreml' else 'explicit_cpu_session',
            'coreml_kernel_events': coreml_count, 'cpu_kernel_events': cpu_count,
            'coreml_ep_executed': coreml_count > 0,
            'cpu_ep_executed': cpu_count > 0 if provider == 'coreml' else aggregate['status'] == 'executed',
            'observed_providers': ([name for name, count in ((COREML, coreml_count), (CPU, cpu_count)) if count]
                                   if provider == 'coreml' else [CPU] if aggregate['status'] == 'executed' else []),
            'evidence_reused': any(c['evidence_reused'] for c in children) if provider == 'coreml' else False,
            'shape_cache_scope': 'exact_input_shape_lru',
            'cached_shape_session_count': len(aggregate['cached_shape_session_ids']), 'shape_sessions': children}
    actual = COREML if provider == 'coreml' else CPU
    request = dict(record['request'], provider=provider, id=record['request_id'])
    evidence = {'schema_version': 1, 'requested_provider': provider, 'actual_provider': actual,
        'compute_units': 'CPUAndGPU' if provider == 'coreml' else 'CPUOnly', 'ane_allowed': False,
        'gpu_execution_verified': False, 'sessions': sessions,
        'coreml_ep_executed': any(s and s['coreml_ep_executed'] for s in sessions.values()),
        'cpu_fallback_observed': provider == 'coreml' and any(s and s['cpu_ep_executed'] for s in sessions.values())}
    response = {'id': record['request_id'], 'ok': True, 'requested_provider': provider, 'actual_provider': actual,
        'execution_window': window(record['started'], record['ended']), 'execution_evidence': evidence,
        'result': [[], []] if record['result_empty'] else None}
    return {'worker_pid': record['process_pid'], 'request': request, 'response': response}


def validate_python_coreml_record(record, *, provider=None):
    """Validate source-only model/runtime/profile evidence; never a ZIP receipt."""
    from backend.tools.native_ocr import validate_execution_evidence
    import re
    if (not isinstance(record, dict) or type(record.get('schema')) is not int or record['schema'] != 1
            or record.get('scope') != 'python-source-inference' or record.get('backend') != 'python-coreml'
            or record.get('provider') not in ('cpu', 'coreml') or (provider is not None and record['provider'] != provider)
            or record.get('gpu_execution_verified') is not False or record.get('internal_coreml_precision_verified') is not False):
        raise ValueError('Invalid Python source inference evidence identity')
    digest = lambda value: isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)
    for name in ('process_pid', 'engine_id', 'request_id'):
        if type(record.get(name)) is not int or not 0 < record[name] < 2**63:
            raise ValueError('Invalid Python source process/request identity')
    if (not digest(record.get('interpreter_sha256')) or not isinstance(record.get('interpreter'), str)
            or not digest(record.get('input_encoded_png_sha256'))
            or type(record.get('input_encoded_png_bytes')) is not int or not 0 < record['input_encoded_png_bytes'] <= 32 * 1024 * 1024
            or type(record.get('result_empty')) is not bool):
        raise ValueError('Invalid Python source interpreter/input identity')
    if record.get('source_files') != _source_identity():
        raise ValueError('Python source producer modules differ from this reviewed checkout')
    runtime = record.get('runtime', {})
    pins = json.loads(Path(__file__).with_name('python_coreml_runtime.json').read_bytes())['wheels'].values()
    matching = [row for row in pins if row['filename'] == runtime.get('wheel_filename')]
    if (len(matching) != 1 or runtime.get('version') != ORT_VERSION
            or runtime.get('wheel_sha256') != matching[0]['sha256']
            or runtime.get('native_files') != matching[0]['native_files']
            or runtime.get('telemetry_disabled_before_import') is not True):
        raise ValueError('Python source runtime differs from the official wheel pin')
    available = runtime.get('available_providers')
    if (not isinstance(available, list) or any(not isinstance(name, str) for name in available)
            or len(set(available)) != len(available) or CPU not in available
            or (record['provider'] == 'coreml' and (COREML not in available
                or matching[0]['filename'] != json.loads(Path(__file__).with_name('python_coreml_runtime.json').read_bytes())['wheels']['darwin-arm64-cp312']['filename']))):
        raise ValueError('Python source provider is incompatible with its official platform wheel')
    for clock in (record.get('started'), record.get('ended')):
        if (not isinstance(clock, dict) or clock.get('process_pid') != record['process_pid']
                or any(type(clock.get(key)) is not int or clock[key] < 0 for key in ('utc_ns', 'monotonic_ns'))):
            raise ValueError('Invalid Python source inference clock')
    request = record.get('request', {})
    if (request.get('tier') not in ('small', 'medium') or request.get('operation') not in ('ocr', 'detect')
            or type(request.get('batch_size')) is not int or not 1 <= request['batch_size'] <= 100
            or type(request.get('cpu_threads')) is not int or not 1 <= request['cpu_threads'] <= 128):
        raise ValueError('Invalid Python source request settings')
    fixed_bytes = (Path(__file__).resolve().parents[2] / 'native/ocr/model-source-pins.json').read_bytes()
    if hashlib.sha256(fixed_bytes).hexdigest() != MODEL_SOURCE_PINS_SHA256:
        raise ValueError('Source verifier model trust root differs from fixed pins')
    fixed = json.loads(fixed_bytes)
    expected = {row['name']: row['onnx_sha256'] for row in fixed['models']}
    required = {f"PP-OCRv6_{request['tier']}_det"}
    if request['operation'] != 'detect':
        required.add(request.get('rec_model'))
    if record.get('model_sha256') != {name: expected.get(name) for name in required} or not required <= set(expected):
        raise ValueError('Python source model evidence differs from the fixed FP32 graphs')
    sessions = record.get('sessions')
    if not isinstance(sessions, dict) or set(sessions) != {'det', 'rec'}:
        raise ValueError('Missing Python source session evidence')
    all_ids = set()
    for role, aggregate in sessions.items():
        if aggregate is None:
            if role == 'det' or (role == 'rec' and request['operation'] != 'detect' and not record['result_empty']):
                raise ValueError('Missing actual Python source session')
            continue
        if not isinstance(aggregate, dict):
            raise ValueError('Invalid source shape aggregate')
        current, total = aggregate.get('request_run_count'), aggregate.get('session_run_count')
        children, cached = aggregate.get('shape_sessions'), aggregate.get('cached_shape_session_ids')
        capacity = 2 if role == 'det' else 4
        if (aggregate.get('gpu_execution_verified') is not False or aggregate.get('capture') != 'per_shape_first_successful_run'
                or type(current) is not int or type(total) is not int or not 0 <= current <= total < 2**63
                or not isinstance(children, list) or len(children) > current
                or aggregate.get('status') != ('executed' if current else 'not_run')
                or bool(children) != bool(current) or aggregate.get('cache_capacity') != capacity
                or not isinstance(cached, list) or len(cached) > min(capacity, total)
                or any(type(sid) is not int or not 0 < sid < 2**63 for sid in cached)
                or len(cached) != len(set(cached)) or (current and not cached)):
            raise ValueError('Invalid Python source shape aggregate/count/cache')
        ids, runs, cumulative = set(), 0, 0
        for row in children:
            sid, count, lifetime = row.get('shape_session_id'), row.get('request_run_count'), row.get('session_run_count')
            if (type(sid) is not int or not 0 < sid < 2**63 or sid in ids
                    or type(count) is not int or type(lifetime) is not int or not 0 < count <= lifetime < 2**63
                    or row.get('evidence_reused') is not (lifetime > count)
                    or row.get('gpu_execution_verified') is not False or row.get('require_static_input_shapes') is not True
                    or row.get('free_dimension_overrides') != _shape_bindings(role, row.get('input_shape', []), request['batch_size'])
                    or any(type(value) is not int for value in row['free_dimension_overrides'].values())):
                raise ValueError('Invalid Python source exact shape identity/count/binding')
            for clock in (row.get('profile_started'), row.get('profile_ended')):
                if (not isinstance(clock, dict) or clock.get('process_pid') != record['process_pid']
                        or any(type(clock.get(key)) is not int or clock[key] < 0 for key in ('utc_ns', 'monotonic_ns'))):
                    raise ValueError('Python first-run evidence belongs to another process/clock')
            first, last = row['profile_started'], row['profile_ended']
            if (first['utc_ns'] > last['utc_ns'] or first['monotonic_ns'] > last['monotonic_ns']
                    or last['monotonic_ns'] > record['ended']['monotonic_ns']
                    or (row['evidence_reused'] and last['monotonic_ns'] > record['started']['monotonic_ns'])
                    or (not row['evidence_reused'] and first['monotonic_ns'] < record['started']['monotonic_ns'])):
                raise ValueError('Python first-run evidence does not match this request interval')
            counts = row.get('kernel_events')
            if (not isinstance(counts, dict) or set(counts) != {COREML, CPU}
                    or any(type(n) is not int or not 0 <= n <= 1000000000 for n in counts.values())
                    or row.get('coreml_ep_executed') is not (counts[COREML] > 0)
                    or row.get('cpu_partition_observed') is not (counts[CPU] > 0)):
                raise ValueError('Python provider flags disagree with actual kernel-event counts')
            if record['provider'] == 'coreml':
                if not digest(row.get('profile_sha256')) or row.get('source') != 'ort_first_run_profile' or not counts[COREML]:
                    raise ValueError('Missing actual Python CoreML first-run profile digest/execution')
            elif (row.get('profile_sha256') is not None or row.get('source') != 'explicit_cpu_session'
                  or counts != {COREML: 0, CPU: 0}):
                raise ValueError('CPU source execution must not fabricate EP profile evidence')
            ids.add(sid); runs += count; cumulative += lifetime
        if runs != current or cumulative > total or (role == 'det' and (current != 1 or len(children) != 1)):
            raise ValueError('Python source aggregate does not count actual tensor executions')
        role_ids = ids | set(cached)
        if all_ids & role_ids:
            raise ValueError('Python shape session identity reused across model roles')
        all_ids |= role_ids
    # Blank recognition still has an explicit not_run identity in the pure
    # validator; no session construction or inference is invented for it.
    canonical = _canonical_evidence(record)
    if sessions['rec'] is None and request['operation'] == 'ocr':
        canonical['response']['execution_evidence']['sessions']['rec'] = {
            'model': request['rec_model'], 'status': 'not_run',
            'source': 'ort_first_run_profile' if record['provider'] == 'coreml' else 'explicit_cpu_session',
            'capture': 'per_shape_first_successful_run' if record['provider'] == 'coreml' else 'explicit_cpu_session',
            'coreml_ep_executed': False, 'cpu_ep_executed': False, 'coreml_kernel_events': 0, 'cpu_kernel_events': 0,
            'observed_providers': [], 'gpu_execution_verified': False, 'request_run_count': 0, 'session_run_count': 0,
            'evidence_reused': False, 'shape_sessions': [], 'cache_capacity': 4,
            'cached_shape_session_count': 0, 'cached_shape_session_ids': [], 'shape_cache_scope': 'exact_input_shape_lru'}
    validate_execution_evidence(canonical['response'], canonical['request'], worker_pid=record['process_pid'])
    return {session['model'] for session in sessions.values() if session and session['status'] == 'executed'}


def validate_python_coreml_records(records, *, provider=None):
    """Complete per-engine records; fresh profiles cannot inherit another engine."""
    seen, covered, ordered = set(), set(), []
    for record in records:
        covered |= validate_python_coreml_record(record, provider=provider)
        identity = record['process_pid'], record['engine_id'], record['request_id']
        if identity in seen:
            raise ValueError('Duplicated Python source request evidence')
        seen.add(identity)
        ordered.append((identity, record))
    previous, ordinal, end_ns, key = None, 0, 0, None
    cache, totals, highest = {'det': {}, 'rec': {}}, {'det': 0, 'rec': 0}, 0
    for identity, record in sorted(ordered, key=lambda row: row[0]):
        engine = identity[:2]
        this_key = json.dumps([record['provider'], record['request']], sort_keys=True)
        if engine != previous:
            ordinal, end_ns, key, highest = 0, 0, this_key, 0
            cache, totals = {'det': {}, 'rec': {}}, {'det': 0, 'rec': 0}
        if identity[2] != ordinal + 1 or this_key != key:
            raise ValueError('Incomplete or inconsistent Python source engine/request sequence')
        if record['started']['monotonic_ns'] < end_ns:
            raise ValueError('Python source request intervals overlap or run backwards')
        fresh, next_cache = set(), {}
        for role, aggregate in record['sessions'].items():
            if aggregate is None:
                if totals[role] or cache[role]:
                    raise ValueError('Python source dispatcher vanished mid-engine')
                next_cache[role] = {}; continue
            if aggregate['session_run_count'] != totals[role] + aggregate['request_run_count']:
                raise ValueError('Python source dispatcher cumulative count changed')
            available = dict(cache[role])
            for child in aggregate['shape_sessions']:
                sid = child['shape_session_id']
                fixed = {k: v for k, v in child.items() if k not in ('request_run_count', 'session_run_count', 'evidence_reused')}
                old = cache[role].get(sid)
                if old is None:
                    if sid <= highest or sid in fresh or child['evidence_reused'] or child['session_run_count'] != child['request_run_count']:
                        raise ValueError('Python source reused unknown/evicted shape session evidence')
                    fresh.add(sid)
                elif (fixed != old['fixed'] or not child['evidence_reused']
                        or child['session_run_count'] != old['runs'] + child['request_run_count']):
                    raise ValueError('Python source changed cached shape/first-run proof/count')
                available[sid] = {'fixed': fixed, 'runs': child['session_run_count']}
            if any(sid not in available for sid in aggregate['cached_shape_session_ids']):
                raise ValueError('Python source cache contains an unobserved shape')
            next_cache[role] = {sid: available[sid] for sid in aggregate['cached_shape_session_ids']}
            totals[role] = aggregate['session_run_count']
        highest = max({highest} | fresh); cache = next_cache
        previous, ordinal, end_ns = engine, identity[2], record['ended']['monotonic_ns']
    return covered
