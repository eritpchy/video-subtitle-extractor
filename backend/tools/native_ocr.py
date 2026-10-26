"""Native OCR subprocess adapter; never imports Python Paddle/ONNX Runtime.

VSE_OCR_BACKEND=native selects the packaged worker and fixed FP32 models.
VSE_NATIVE_OCR_PROVIDER=coreml requests the macOS CoreML CPU+GPU path; explicit
CPU mode overrides it. Missing providers or failed execution never silently
retry on CPU. Provider execution is not proof of physical GPU-only execution.
No runtime/model download occurs here.
"""
from collections import deque
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import weakref


def _record_worker_audit(process, executable, phase):
    """Use only an already-installed verification probe; ordinary runs do no audit.

    The isolated bundle bootstrap authenticates this module before installation.
    No environment flag can import an arbitrary hook or select a worker path.
    """
    if sys.platform != 'win32':
        return
    for name in ('scripts.standalone_runtime_probe', 'standalone_runtime_probe'):
        probe = sys.modules.get(name)
        if probe is not None and getattr(probe, '_NATIVE_WORKER_AUDIT', None) is not None:
            probe.record_native_worker(process.pid, executable, phase)
            return


VALIDATED_LEGACY_MODELS = frozenset({
    'arabic_PP-OCRv5_mobile_rec', 'cyrillic_PP-OCRv5_mobile_rec',
    'devanagari_PP-OCRv5_mobile_rec', 'el_PP-OCRv5_mobile_rec',
    'eslav_PP-OCRv5_mobile_rec', 'korean_PP-OCRv5_mobile_rec',
    'latin_PP-OCRv5_mobile_rec', 'th_PP-OCRv5_mobile_rec',
    'ta_PP-OCRv5_mobile_rec', 'te_PP-OCRv5_mobile_rec',
    'ka_PP-OCRv3_mobile_rec',  # Project kn/ka are both Kannada aliases, not Georgian.
})


def selected_backend():
    backend = os.environ.get('VSE_OCR_BACKEND', 'paddle').strip().lower()
    if backend not in ('paddle', 'native', 'directml'):
        raise ValueError(f'Unsupported VSE_OCR_BACKEND: {backend}')
    if backend == 'native':
        configured_native_provider()
    return backend


def configured_native_provider():
    """The package/requested provider, not an availability or execution claim."""
    provider = os.environ.get('VSE_NATIVE_OCR_PROVIDER', 'cpu').strip().lower()
    if provider not in ('cpu', 'coreml'):
        raise ValueError(f'Unsupported native OCR provider: {provider}; expected cpu or coreml')
    return provider


def selected_native_provider(*, enabled=True):
    provider = configured_native_provider()
    if not enabled:
        return 'cpu'  # Explicit --cpu / GUI-disable, never an inferred fallback.
    if provider == 'coreml' and sys.platform != 'darwin':
        raise ValueError('Native CoreML requires macOS; select explicit CPU mode on this host')
    return provider


def _worker_location(root=None):
    value = root or os.environ.get('VSE_NATIVE_OCR_ROOT')
    if not value:
        raise ValueError('Native OCR requires VSE_NATIVE_OCR_ROOT; no runtime is downloaded')
    directory = Path(value).expanduser().resolve()
    executable = directory / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    if not executable.is_file():
        raise FileNotFoundError(f'Missing native OCR executable: {executable}')
    return directory, executable


def _worker_environment(root):
    environment = dict(os.environ, ORT_DISABLE_TELEMETRY='1', VSE_OCR_PARENT_PID=str(os.getpid()))
    if os.name != 'nt':
        key = 'DYLD_LIBRARY_PATH' if sys.platform == 'darwin' else 'LD_LIBRARY_PATH'
        environment[key] = str(root) + (os.pathsep + environment[key] if environment.get(key) else '')
    return environment


def _read_capabilities(stream, answers):
    try:
        data = bytearray()
        while len(data) <= 65536:
            block = stream.read(65537 - len(data))
            if not block:
                break
            data.extend(block)
        answers.put(bytes(data))  # One bounded JSON capability document.
    except BaseException as error:
        answers.put(error)


def native_capabilities(root=None, *, timeout=10):
    """Probe the real bundled worker, without importing a Python ORT package.

    Availability is not GPU execution evidence. Output, stderr and time are
    bounded, and a missing/malformed capability response is a hard failure.
    """
    directory, executable = _worker_location(root)
    if type(timeout) not in (int, float) or not 0 < timeout <= 30:
        raise ValueError('Native capability timeout must be in (0, 30] seconds')
    process = subprocess.Popen([str(executable), '--capabilities'], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=_worker_environment(directory),
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    tail, answers = deque(maxlen=16), queue.Queue(maxsize=1)
    stderr = threading.Thread(target=_drain_stderr, args=(process.stderr, tail), daemon=True)
    stdout = threading.Thread(target=_read_capabilities, args=(process.stdout, answers), daemon=True)
    deadline = time.monotonic() + timeout
    stderr.start(); stdout.start()
    try:
        try:
            data = answers.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError('Native OCR capability probe timed out') from error
        if isinstance(data, BaseException):
            raise RuntimeError('Native OCR capability transport failed') from data
        if len(data) > 65536:
            raise ValueError('Native OCR capability response exceeds its bounded JSON contract')
        try:
            returncode = process.wait(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('Native OCR capability process did not exit') from error
        if returncode:
            diagnostic = b''.join(list(tail))[-4096:].decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'Native OCR capability probe failed ({returncode}): {diagnostic}')
        if not data:
            raise ValueError('Native OCR capability response is empty')
        document = json.loads(data, object_pairs_hook=_unique_object)
        return validate_native_capabilities(document)
    finally:
        _reap(process)
        stdout.join(timeout=1); stderr.join(timeout=1)


def validate_native_capabilities(document):
    if (not isinstance(document, dict) or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or not isinstance(document.get('ort_version'), str) or not document['ort_version']
            or document.get('default_request_provider') != 'cpu'):
        raise ValueError('Invalid native OCR capabilities')
    for key in ('available_providers', 'supported_request_providers'):
        values = document.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value for value in values) or len(set(values)) != len(values):
            raise ValueError(f'Invalid native OCR capability field: {key}')
    providers = document['supported_request_providers']
    if not set(providers) <= {'cpu', 'coreml'} or 'cpu' not in providers or 'CPUExecutionProvider' not in document['available_providers']:
        raise ValueError('Unsupported native OCR capability provider contract')
    coreml = document.get('coreml')
    if (not isinstance(coreml, dict) or type(coreml.get('available')) is not bool
            or coreml.get('model_format') != 'MLProgram' or coreml.get('compute_units') != 'CPUAndGPU'
            or coreml.get('ane_allowed') is not False or coreml.get('gpu_execution_verified') is not False):
        raise ValueError('Capability discovery must declare MLProgram CPUAndGPU without ANE/GPU execution claims')
    available = ('coreml' in providers and 'CoreMLExecutionProvider' in document['available_providers'] and document['ort_version'] == '1.31.0')
    if coreml['available'] != available or ('coreml' in providers) != available:
        raise ValueError('CoreML capability identity/version/provider mismatch')
    return document


def require_coreml_capabilities(root=None):
    if sys.platform != 'darwin':
        raise ValueError('Native CoreML requires macOS; automatic CPU fallback is disabled')
    document = native_capabilities(root)
    if 'coreml' not in document['supported_request_providers'] or 'CoreMLExecutionProvider' not in document['available_providers']:
        raise RuntimeError('Native CoreML provider is unavailable; automatic CPU fallback is disabled (explicit CPU mode remains available)')
    return document


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate native OCR JSON field: {key}')
        result[key] = value
    return result


def emit_request_evidence(document, directory):
    """Write opt-in acceptance evidence, never ordinary user OCR output.

    The caller must control the existing directory. Reject existing files and
    symlink/reparse paths; POSIX creation is relative to an opened directory.
    This is not a cross-platform defense against an attacker concurrently
    replacing Windows directory ancestors. Full response text is retained only
    when VSE_NATIVE_EVIDENCE_DIR is explicitly set for acceptance diagnostics.
    A write error is fatal, and a partial newly created file is deliberately not
    deleted by pathname (which might no longer identify our file).
    """
    if not isinstance(document, dict) or not isinstance(document.get('request'), dict):
        raise ValueError('Invalid native request evidence document')
    worker_pid, request_id = document.get('worker_pid'), document['request'].get('id')
    if any(type(value) is not int or not 0 < value < 2**63 for value in (worker_pid, request_id)):
        raise ValueError('Invalid native request evidence PID/request identity')
    data = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > 17 * 1024 * 1024:
        raise ValueError('Native request evidence exceeds its bounded JSON contract')
    if not isinstance(directory, (str, os.PathLike)) or not str(directory).strip():
        raise ValueError('Native evidence directory must be an existing local directory')
    path = Path(directory).expanduser()
    if '..' in path.parts or str(path).startswith('\\\\'):
        raise ValueError('Native evidence directory must not use parent traversal or a device/UNC path')
    path = path.absolute()
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Native evidence directory must not contain symlink/reparse ancestors')
    filename = f'native-ocr-{worker_pid}-{request_id}.json'
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    directory_fd = None
    try:
        if os.open in os.supports_dir_fd:
            directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(directory_fd)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Native evidence directory changed while opening')
            descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
        else:
            descriptor = os.open(path / filename, flags, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return path / filename


def _validated_execution_window(value, *, worker_pid=None):
    if not isinstance(value, dict):
        raise ValueError('Invalid native execution window')
    for field in ('worker_pid', 'utc_start_ns', 'utc_end_ns', 'monotonic_start_ns', 'monotonic_end_ns'):
        if type(value.get(field)) is not int or value[field] < 0:
            raise ValueError('Invalid native execution window identity/timestamp')
    if (value['worker_pid'] < 1 or (worker_pid is not None and value['worker_pid'] != worker_pid)
            or value['utc_end_ns'] < value['utc_start_ns'] or value['monotonic_end_ns'] < value['monotonic_start_ns']):
        raise ValueError('Native execution window does not match its worker/ordered timestamps')
    return value


def validate_execution_evidence(response, request, *, worker_pid=None):
    """Validate actual native EP evidence; never convert configuration into GPU proof."""
    if 'execution_window' in response:
        _validated_execution_window(response['execution_window'], worker_pid=worker_pid)
    provider = request['provider']
    actual = 'CoreMLExecutionProvider' if provider == 'coreml' else 'CPUExecutionProvider'
    if response.get('requested_provider') != provider or response.get('actual_provider') != actual:
        raise ValueError('Native OCR did not honor requested/actual provider; silent CPU fallback refused')
    evidence = response.get('execution_evidence')
    if (not isinstance(evidence, dict) or type(evidence.get('schema_version')) is not int or evidence['schema_version'] != 1
            or evidence.get('requested_provider') != provider or evidence.get('actual_provider') != actual
            or evidence.get('compute_units') != ('CPUAndGPU' if provider == 'coreml' else 'CPUOnly')
            or evidence.get('ane_allowed') is not False or evidence.get('gpu_execution_verified') is not False):
        raise ValueError('Invalid native provider execution evidence or unsupported ANE/GPU claim')
    sessions = evidence.get('sessions')
    if not isinstance(sessions, dict) or set(sessions) != {'det', 'rec'}:
        raise ValueError('Native execution evidence requires detection and recognition session identities')
    expected_models = {'det': f"PP-OCRv6_{request['tier']}_det", 'rec': request['rec_model']}
    observed_coreml, observed_cpu = False, False
    for role in ('det', 'rec'):
        session = sessions[role]
        if role == 'rec' and request['operation'] == 'detect':
            if session is not None:
                raise ValueError('Detection-only evidence must not claim recognition execution')
            continue
        if not isinstance(session, dict) or session.get('model') != expected_models[role]:
            raise ValueError(f'Native execution evidence model mismatch: {role}')
        status = session.get('status')
        if status not in ('executed', 'not_run') or (status == 'not_run' and (role != 'rec' or response.get('result') != [[], []])):
            raise ValueError(f'Invalid native session execution status: {role}')
        if session.get('source') != ('ort_first_run_profile' if provider == 'coreml' else 'explicit_cpu_session'):
            raise ValueError('Native session execution evidence has an unsupported source')
        if 'profile_window' in session:
            _validated_execution_window(session['profile_window'], worker_pid=worker_pid)
        if 'capture' in session and session['capture'] != ('first_successful_run' if provider == 'coreml' else 'explicit_cpu_session'):
            raise ValueError('Native execution evidence capture scope is inconsistent')
        if 'request_run_count' in session or 'session_run_count' in session:
            current, cumulative = session.get('request_run_count'), session.get('session_run_count')
            if (type(current) is not int or type(cumulative) is not int or not 0 <= current <= cumulative
                    or (status == 'executed') != (current > 0)):
                raise ValueError('Native session/request run counts disagree with execution status')
            if session.get('evidence_reused') is not (provider == 'coreml' and current > 0 and cumulative > current):
                raise ValueError('Native reused first-run evidence is mislabeled')
        if session.get('gpu_execution_verified') is not False:
            raise ValueError('ORT provider profiling is not physical GPU execution verification')
        counts = {}
        for key in ('coreml_kernel_events', 'cpu_kernel_events'):
            value = session.get(key)
            if type(value) is not int or not 0 <= value <= 1000000000:
                raise ValueError('Invalid native provider kernel-event count')
            counts[key] = value
        coreml_executed = counts['coreml_kernel_events'] > 0
        cpu_executed = counts['cpu_kernel_events'] > 0 if provider == 'coreml' else status == 'executed'
        if (type(session.get('coreml_ep_executed')) is not bool or session['coreml_ep_executed'] != coreml_executed
                or type(session.get('cpu_ep_executed')) is not bool or session['cpu_ep_executed'] != cpu_executed):
            raise ValueError('Native provider execution flags disagree with session evidence')
        if status == 'not_run' and any(counts.values()):
            raise ValueError('Unexecuted native session must not report kernel events')
        if provider == 'cpu' and any(counts.values()):
            raise ValueError('Explicit CPU session must not fabricate profile events')
        if provider == 'coreml' and status == 'executed' and not coreml_executed:
            raise ValueError('No CoreML kernel executed; all-CPU fallback refused')
        expected = ({'CoreMLExecutionProvider'} if coreml_executed else set()) | ({'CPUExecutionProvider'} if cpu_executed else set())
        observed = session.get('observed_providers')
        if (not isinstance(observed, list) or any(not isinstance(item, str) for item in observed)
                or len(observed) != len(set(observed)) or set(observed) != expected):
            raise ValueError('Native observed providers disagree with executed sessions')
        observed_coreml |= coreml_executed
        observed_cpu |= cpu_executed
    if (type(evidence.get('coreml_ep_executed')) is not bool or evidence['coreml_ep_executed'] != observed_coreml
            or type(evidence.get('cpu_fallback_observed')) is not bool
            or evidence['cpu_fallback_observed'] != (provider == 'coreml' and observed_cpu)):
        raise ValueError('Native aggregate provider evidence disagrees with session evidence')
    return evidence


def _reap(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream:
            stream.close()


def _read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        block = stream.read(size - len(chunks))
        if not block:
            raise EOFError('Native OCR worker closed its response stream')
        chunks.extend(block)
    return bytes(chunks)


def _exchange(process, payload, answers):
    try:
        packet = memoryview(struct.pack('<I', len(payload)) + payload)
        while packet:
            count = process.stdin.write(packet)
            if not count:
                raise EOFError('Native OCR worker closed its request stream')
            packet = packet[count:]
        process.stdin.flush()
        size, = struct.unpack('<I', _read_exact(process.stdout, 4))
        if not 0 < size <= 16 * 1024 * 1024:
            raise ValueError('Native OCR response exceeds the 16 MiB protocol limit')
        answers.put(json.loads(_read_exact(process.stdout, size), object_pairs_hook=_unique_object))
    except BaseException as error:
        answers.put(error)


def _drain_stderr(stream, tail):
    try:
        while True:
            block = stream.read(4096)
            if not block:
                return
            tail.append(block)
    except (OSError, ValueError):
        pass


class NativeOcr:
    """One sequential, long-lived worker with bounded transport and cancellation."""
    def __init__(self, model_config, batch_number=6, *, stop_event=None, root=None, detection_only=False):
        selected_backend()  # Validate explicit provider even with injected hardware.
        self.provider = selected_native_provider(enabled=model_config.hardware_accelerator.is_enabled)
        self.execution_evidence = None
        self.execution_window = None
        self.last_request_evidence = None
        self._worker_sha256 = None
        self._evidence_directory = os.environ.get('VSE_NATIVE_EVIDENCE_DIR')
        self._reported_evidence = False
        self._capabilities = None
        self.model_name = model_config.REC_MODEL_NAME
        self.detection_only = detection_only
        if not detection_only and self.model_name not in VALIDATED_LEGACY_MODELS | {
                f'PP-OCRv6_{model_config.TIER}_rec'}:
            raise ValueError(f'Native OCR model is not validated: {self.model_name}; no language substitution allowed')
        if model_config.hardware_accelerator.has_cuda():
            raise ValueError('Native OCR cannot satisfy a requested CUDA device')
        self.tier = model_config.TIER
        self.batch_number = int(batch_number)
        if not 1 <= self.batch_number <= 100:
            raise ValueError('Native OCR batch size must be in 1..100')
        self.cpu_threads = int(os.environ.get('VSE_OCR_CPU_THREADS', min(4, os.cpu_count() or 1)))
        if not 1 <= self.cpu_threads <= 128:
            raise ValueError('VSE_OCR_CPU_THREADS must be in 1..128')
        self.timeout = float(os.environ.get('VSE_NATIVE_OCR_TIMEOUT', '120'))
        if not 0 < self.timeout <= 120:
            raise ValueError('VSE_NATIVE_OCR_TIMEOUT must be in (0, 120] seconds')
        self.root, self.executable = _worker_location(root)
        self._validate_assets()
        self.stop_event = stop_event
        self._process = None
        self._finalizer = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._sequence = 0
        self._stderr_thread = None
        self._stderr = deque(maxlen=16)  # 64 KiB maximum diagnostic tail.

    def _validate_assets(self):
        manifest = json.loads((self.root / 'model-manifest.json').read_text(encoding='utf-8'))
        if (not isinstance(manifest, list) or any(not isinstance(item, dict)
                or not isinstance(item.get('name'), str) for item in manifest)):
            raise ValueError('Invalid native model manifest')
        entries = {item['name']: item for item in manifest}
        if len(entries) != len(manifest):
            raise ValueError('Duplicate native model names in manifest')
        configuration = json.loads((self.root / 'config' / f'{self.tier}.json').read_text(encoding='utf-8'))
        for role in (('det',) if self.detection_only else ('det', 'rec')):
            name = f'PP-OCRv6_{self.tier}_det' if role == 'det' else self.model_name
            role_config = configuration[role]
            if role == 'rec' and not name.startswith('PP-OCRv6_'):
                config_path = self.root / 'config' / f'{name}.json'
                if not config_path.is_file():
                    raise FileNotFoundError(f'Native OCR has no installed config for {name}; no language substitution allowed')
                role_config = json.loads(config_path.read_text(encoding='utf-8'))
            if role_config['Global']['model_name'] != name:
                raise ValueError(f'Native OCR model identity mismatch: {name}')
            path = self.root / 'models' / f'{name}_infer.onnx'
            if not path.is_file():
                raise FileNotFoundError(f'Missing native model: {path}; no model substitution allowed')
            entry = entries.get(f'{name}_infer', {})
            config_file = (f'config/{name}.json' if role == 'rec' and not name.startswith('PP-OCRv6_')
                           else f'config/{self.tier}.json')
            config_bytes = (self.root / config_file).read_bytes()
            if (entry.get('config_file') != config_file
                    or entry.get('config_sha256') != hashlib.sha256(config_bytes).hexdigest()):
                raise ValueError(f'Native OCR config checksum mismatch: {config_file}')
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if entry.get('onnx_bytes') != path.stat().st_size or entry.get('onnx_sha256') != digest.hexdigest():
                raise ValueError(f'Native OCR model checksum mismatch: {path}')

    def _start(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError('Native OCR recognizer is closed')
            if self._process is not None:
                return self._process
            if self.provider == 'coreml':
                self._capabilities = require_coreml_capabilities(self.root)
            # Bind the on-disk worker used for this process, not an assumed build
            # identifier. This is artifact correlation, not loaded-memory
            # attestation; models remain bound by their validated manifest.
            digest = hashlib.sha256()
            with self.executable.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            self._worker_sha256 = digest.hexdigest()
            environment = _worker_environment(self.root)
            process = subprocess.Popen([str(self.executable), '--models', str(self.root / 'models'),
                                        '--config', str(self.root / 'config')],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, bufsize=0, env=environment,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self._process = process
            self._finalizer = weakref.finalize(self, _reap, process)
            self._stderr.clear()
            self._stderr_thread = threading.Thread(target=_drain_stderr,
                args=(process.stderr, self._stderr), name='vse-native-stderr', daemon=True)
            self._stderr_thread.start()
            try:
                _record_worker_audit(process, self.executable, 'start')
            except BaseException:
                # _start runs before the request transport's cleanup handler.
                _reap(process)
                self._stderr_thread.join(timeout=1)
                self._stderr_thread = None
                self._finalizer.detach()
                self._finalizer = None
                self._process = None
                raise
            return process

    def _discard(self):
        self.execution_evidence = None
        self.execution_window = None
        self.last_request_evidence = None
        self._reported_evidence = False
        with self._state_lock:
            if self._process is not None:
                _reap(self._process)
                if self._stderr_thread is not None:
                    self._stderr_thread.join(timeout=1)
                    self._stderr_thread = None
                # Keep the finalizer alive if reaping raises, so cleanup may be
                # retried rather than losing ownership of an outstanding child.
                if self._finalizer is not None:
                    self._finalizer.detach()
                self._finalizer = None
                self._process = None

    def close(self):
        # May be called from another thread during a blocked request.
        with self._state_lock:
            self._closed = True
            process = self._process
        try:
            if process is not None:
                _record_worker_audit(process, self.executable, 'close')
        finally:
            self._discard()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def _request(self, path, operation='ocr', *, image_identity):
        with self._lock:
            self.execution_evidence = None
            self.execution_window = None
            self.last_request_evidence = None
            if self.stop_event is not None and self.stop_event.is_set():
                raise RuntimeError('Native OCR cancelled')
            process = self._start()
            self._sequence += 1
            request = {'id': self._sequence, 'tier': self.tier, 'path': str(path),
                       'timeout_ms': max(1, int(self.timeout * 1000)), 'provider': self.provider,
                       'batch_size': self.batch_number, 'operation': operation,
                       'cpu_threads': self.cpu_threads, 'rec_model': self.model_name}
            payload = json.dumps(request, ensure_ascii=False).encode('utf-8')
            if len(payload) > 65536:
                raise ValueError('Native OCR request exceeds 65536 bytes')
            answers = queue.Queue(maxsize=1)
            thread = threading.Thread(target=_exchange, args=(process, payload, answers),
                                      name='vse-native-request', daemon=True)
            deadline = time.monotonic() + self.timeout
            thread.start()
            try:
                while True:
                    if self._closed or (self.stop_event is not None and self.stop_event.is_set()):
                        raise RuntimeError('Native OCR cancelled')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Native OCR exceeded {self.timeout:g} seconds')
                    try:
                        response = answers.get(timeout=min(.05, remaining))
                        break
                    except queue.Empty:
                        continue
                if isinstance(response, BaseException):
                    raise RuntimeError(f'Native OCR transport failed: {response}') from response
                if not isinstance(response, dict) or type(response.get('id')) is not int or response.get('id') != self._sequence or type(response.get('ok')) is not bool:
                    raise ValueError('Invalid or unaligned native OCR response')
                if not response['ok']:
                    raise RuntimeError(f'Native OCR failed: {response.get("error", "unknown error")}')
                for setting in ('rec_model', 'batch_size', 'cpu_threads'):
                    if type(response.get(setting)) is not type(request[setting]) or response.get(setting) != request[setting]:
                        raise ValueError(f'Native OCR did not honor requested {setting}')
                evidence = validate_execution_evidence(response, request, worker_pid=process.pid)
                self.execution_evidence = json.loads(json.dumps(evidence))
                self.execution_window = json.loads(json.dumps(response.get('execution_window')))
                # In-memory only: the caller/checker may retain this bounded raw
                # response, including first-run profile reuse and current-request
                # window. Never mistake fixture PNG encoding for the exact PNG
                # bytes the worker consumed, or persist user OCR text here.
                retained_request = {key: value for key, value in request.items() if key != 'path'}
                retained_request.update(image_identity)
                self.last_request_evidence = json.loads(json.dumps({
                    'schema_version': 1, 'request': retained_request, 'response': response,
                    'worker_sha256': self._worker_sha256, 'worker_pid': process.pid,
                    'worker_sha256_scope': 'on-disk executable before process launch',
                }))
                if not self._reported_evidence:
                    logging.getLogger(__name__).info(
                        'Native OCR provider evidence: %s; ORT EP/partition evidence is not physical GPU execution proof; CoreML internal CPU use is not measured',
                        json.dumps({'execution_evidence': evidence, 'execution_window': self.execution_window}, sort_keys=True))
                    self._reported_evidence = True
                _record_worker_audit(process, self.executable, 'inference')
                return response
            except BaseException as error:
                self._discard()  # Failed request is never silently retried; next call may restart.
                if isinstance(error, RuntimeError) and 'transport failed' in str(error):
                    tail = b''.join(list(self._stderr))[-4096:].decode('utf-8', errors='replace').strip()
                    if tail:
                        raise RuntimeError(f'{error}; native stderr: {tail}') from error
                raise
            finally:
                thread.join(timeout=1)

    def _image_request(self, image, operation='ocr'):
        self.execution_evidence = None
        self.execution_window = None
        self.last_request_evidence = None
        import cv2
        import numpy as np
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim not in (2, 3):
            raise ValueError('Native OCR expects a uint8 BGR, BGRA or grayscale image')
        height, width = image.shape[:2]
        if (not height or not width or max(height, width) > 16384 or height * width > 16000000
                or (image.ndim == 3 and image.shape[2] not in (3, 4))):
            raise ValueError('Native OCR image dimensions or channels exceed supported limits')
        ok, encoded = cv2.imencode('.png', image)
        if not ok or encoded.nbytes > 32 * 1024 * 1024:
            raise ValueError('Native OCR encoded image exceeds 32 MiB')
        # Close before child opens: Windows does not allow sharing NamedTemporaryFile.
        with tempfile.TemporaryDirectory(prefix='vse-native-ocr-') as temporary:
            path = Path(temporary) / 'frame.png'
            image_bytes = encoded.tobytes()
            path.write_bytes(image_bytes)
            return self._request(path, operation, image_identity={
                'image_sha256': hashlib.sha256(image_bytes).hexdigest(),
                'image_bytes': len(image_bytes), 'image_format': 'png',
            })

    def predict_iter(self, image):
        if self.detection_only:
            raise RuntimeError('Detection-only native worker cannot perform recognition')
        response = self._image_request(image)
        try:
            result = self._recognition_result(response)
            if self._evidence_directory is not None:
                emit_request_evidence(self.last_request_evidence, self._evidence_directory)
        except Exception:
            self._discard()
            raise
        yield result

    def _recognition_result(self, response):
        result = response.get('result')
        if not isinstance(result, list) or len(result) != 2:
            raise ValueError('Invalid native OCR result')
        boxes, records = result
        if (not isinstance(boxes, list) or not isinstance(records, list)
                or len(boxes) != len(records)
                or any(not isinstance(record, list) or len(record) != 2 for record in records)):
            raise ValueError('Unaligned native OCR boxes and recognition')
        for box, (text, score) in zip(boxes, records):
            if (not isinstance(text, str) or type(score) not in (float, int)
                    or not math.isfinite(score)):
                raise ValueError('Invalid native OCR text or confidence')
            if (not isinstance(box, list) or len(box) != 4
                    or any(not isinstance(point, list) or len(point) != 2
                           or any(type(value) not in (float, int) or not math.isfinite(value)
                                  for value in point) for point in box)):
                raise ValueError('Invalid native OCR polygon')
        texts = [r[0] for r in records]
        text_order = response.get('text_order')
        if self.model_name == 'arabic_PP-OCRv5_mobile_rec':
            if text_order == 'logical':
                # Match PaddleX exactly, including its handling of bidi isolates.
                # Never reorder scores/boxes, normalize Unicode, or apply twice.
                from bidi.algorithm import get_display
                texts = [get_display(text) for text in texts]
            elif text_order != 'display':
                raise ValueError('Arabic native response must declare logical/display text_order')
        elif text_order not in (None, 'display'):
            raise ValueError('Unexpected native OCR text_order for non-Arabic model')
        return {'rec_polys': boxes, 'rec_texts': texts,
                'rec_scores': [r[1] for r in records]}

    def predict(self, image):
        import numpy as np
        response = self._image_request(image, 'detect')
        try:
            raw = response['dt_polys']
            if not isinstance(raw, list) or any(
                    not isinstance(box, list) or len(box) != 4
                    or any(not isinstance(point, list) or len(point) != 2
                           or any(type(value) not in (float, int) or not math.isfinite(value)
                                  for value in point) for point in box) for box in raw):
                raise ValueError('Invalid native detection polygons')
            polygons = np.asarray(raw, dtype=np.float32)
            if polygons.size and (polygons.ndim != 3 or polygons.shape[1:] != (4, 2) or not np.isfinite(polygons).all()):
                raise ValueError('Invalid native detection polygons')
            if self._evidence_directory is not None:
                emit_request_evidence(self.last_request_evidence, self._evidence_directory)
            return [{'dt_polys': response['dt_polys']}]
        except Exception:
            self._discard()
            raise
