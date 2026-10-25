"""Opt-in, CPU-only native OCR subprocess adapter; never imports Paddle/ORT.

VSE_OCR_BACKEND=native and VSE_NATIVE_OCR_ROOT=/absolute/staged/worker select
this experimental profile. The default remains Paddle. A native package contains
native-ocr[.exe], models/, config/ and model-manifest.json; no downloads occur.
"""
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time
import weakref


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
    if backend not in ('paddle', 'native'):
        raise ValueError(f'Unsupported VSE_OCR_BACKEND: {backend}')
    if backend == 'native' and os.environ.get('VSE_NATIVE_OCR_PROVIDER', 'cpu').lower() != 'cpu':
        raise ValueError('Native OCR currently supports CPU only; requested GPU provider refused')
    return backend


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
        answers.put(json.loads(_read_exact(process.stdout, size)))
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
        value = root or os.environ.get('VSE_NATIVE_OCR_ROOT')
        if not value:
            raise ValueError('Native OCR requires VSE_NATIVE_OCR_ROOT; no runtime is downloaded')
        self.root = Path(value).expanduser().resolve()
        self.executable = self.root / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
        if not self.executable.is_file():
            raise FileNotFoundError(f'Missing native OCR executable: {self.executable}')
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
            environment = dict(os.environ, ORT_DISABLE_TELEMETRY='1',
                               VSE_OCR_PARENT_PID=str(os.getpid()))
            # Native artifacts resolve their sibling libraries; never invoke a shell.
            if os.name != 'nt':
                key = 'DYLD_LIBRARY_PATH' if __import__('sys').platform == 'darwin' else 'LD_LIBRARY_PATH'
                environment[key] = str(self.root) + (os.pathsep + environment[key] if environment.get(key) else '')
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
            return process

    def _discard(self):
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
        self._discard()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    def _request(self, path, operation='ocr'):
        with self._lock:
            if self.stop_event is not None and self.stop_event.is_set():
                raise RuntimeError('Native OCR cancelled')
            process = self._start()
            self._sequence += 1
            request = {'id': self._sequence, 'tier': self.tier, 'path': str(path),
                       'timeout_ms': max(1, int(self.timeout * 1000)), 'provider': 'cpu',
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
            path.write_bytes(encoded.tobytes())
            return self._request(path, operation)

    def predict_iter(self, image):
        if self.detection_only:
            raise RuntimeError('Detection-only native worker cannot perform recognition')
        response = self._image_request(image)
        try:
            result = self._recognition_result(response)
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
            return [{'dt_polys': response['dt_polys']}]
        except Exception:
            self._discard()
            raise
