#!/usr/bin/env python3
"""Build-time verifier; never included in the native OCR runtime.

The worker protocol is uint32 little-endian length + UTF-8 JSON in both
 directions. No stdout logging is allowed. Existing frozen Python/ORT and
 Paddle baselines are evidence, not a runtime dependency of this verifier.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

MAX_RESPONSE = 16 * 1024 * 1024


def frame(value):
    body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode('utf-8')
    return struct.pack('<I', len(body)) + body


def read_exact(stream, count):
    parts = bytearray()
    while len(parts) < count:
        chunk = stream.read(count - len(parts))
        if not chunk:
            raise EOFError(f'worker EOF: wanted {count}, received {len(parts)} bytes')
        parts.extend(chunk)
    return bytes(parts)


class Worker:
    def __init__(self, command, timeout=120):
        self.timeout = timeout
        self.errors = tempfile.TemporaryFile()
        env = os.environ.copy()
        # Poisoning inherited Python search paths must not affect a native worker.
        env.update(PYTHONHOME='/nonexistent/native-ocr-no-python', PYTHONPATH='/nonexistent/native-ocr-no-python', ORT_DISABLE_TELEMETRY='1', VSE_OCR_PARENT_PID=str(os.getpid()))
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, env=env)
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            while True:
                length, = struct.unpack('<I', read_exact(self.proc.stdout, 4))
                if not 0 < length <= MAX_RESPONSE:
                    raise ValueError(f'invalid response size {length}')
                self.responses.put(json.loads(read_exact(self.proc.stdout, length)))
        except BaseException as exc:
            self.responses.put(exc)

    def request(self, value):
        self.proc.stdin.write(frame(value))
        self.proc.stdin.flush()
        try:
            result = self.responses.get(timeout=self.timeout)
        except queue.Empty:
            self.proc.kill()
            self.proc.wait()
            raise TimeoutError(f'worker request exceeded {self.timeout}s')
        if isinstance(result, BaseException):
            self.errors.seek(0)
            raise RuntimeError(f'{result}; stderr={self.errors.read().decode(errors="replace")[-4000:]}')
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise AssertionError(f'invalid response envelope {result!r}')
        if isinstance(value, dict) and 'id' in value and result.get('id') != value['id']:
            raise AssertionError('response id mismatch')
        return result

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError('worker failed to clean up after EOF')
        self.errors.seek(0)
        stderr = self.errors.read().decode(errors='replace')
        self.errors.close()
        return code, stderr


def compare(actual, expected, tolerance):
    assert isinstance(actual, list) and len(actual) == 2, 'result must contain boxes and recognition'
    boxes, recognition = actual
    assert len(boxes) == len(recognition), 'unaligned boxes/recognition'
    same_boxes = boxes == expected[0]
    same_text = [x[0] for x in recognition] == [x[0] for x in expected[1]]
    delta = max((abs(float(a[1]) - float(b[1])) for a, b in zip(recognition, expected[1])), default=0.0)
    finite = all(math.isfinite(float(x[1])) for x in recognition)
    return {'same_boxes': same_boxes, 'same_text': same_text, 'confidence_delta': delta,
            'finite_confidence': finite, 'passed': same_boxes and same_text and finite and delta <= tolerance}


def native_evidence(pid, executable):
    if sys.platform == 'win32':
        # Direct smoke.py execution has only native/ocr on sys.path. This known
        # source root supplies our standard-library, read-only Windows auditor.
        source_root = str(Path(__file__).resolve().parents[2])
        if source_root not in sys.path:
            sys.path.insert(0, source_root)
        from scripts.windows_native_worker_evidence import collect_native_worker_evidence
        return collect_native_worker_evidence(pid, executable)
    proc = Path('/proc') / str(pid)
    if not proc.exists():
        return {'available': False, 'reason': '/proc process inspection unavailable on this platform'}
    actual_exe = (proc / 'exe').resolve()
    maps = (proc / 'maps').read_text()
    mapped_paths = sorted({line.split(None, 5)[5] for line in maps.splitlines() if len(line.split(None, 5)) == 6 and line.split(None, 5)[5].startswith('/')})
    forbidden = [p for p in mapped_paths if any(s in Path(p).name.lower() for s in ('libpython', 'paddle', 'python3'))]
    return {'available': True, 'executable': str(actual_exe), 'matches_worker': actual_exe.samefile(executable),
            'mapped_paths': mapped_paths,
            'onnxruntime_library_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in mapped_paths if 'libonnxruntime' in Path(p).name},
            'forbidden_python_paddle_libraries': forbidden,
            'passed': actual_exe.samefile(executable) and not forbidden,
            'scope': 'Observed worker executable and loaded libraries after inference; not a network or transitive provenance certification.'}


def raw_case(command, name, data, expect_zero=False):
    try:
        # These malformed-frame probes also load ORT. Set its privacy control
        # before process/library initialization, not just inside worker main or
        # after CreateEnv. Never rely on the caller having run the CI workflow.
        environment = dict(os.environ, ORT_DISABLE_TELEMETRY='1')
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30, env=environment)
        passed = result.returncode == 0 if expect_zero else result.returncode > 0
        return {'name': name, 'passed': passed, 'returncode': result.returncode,
                'stdout_bytes': len(result.stdout), 'stderr': result.stderr.decode(errors='replace')[-2000:]}
    except subprocess.TimeoutExpired:
        return {'name': name, 'passed': False, 'error': 'did not terminate on EOF/invalid frame within 30s'}
