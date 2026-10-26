"""Actual standalone C/C++/fd noise regression; no ORT or model execution."""
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_worker_owns_private_protocol_before_ort_and_never_writes_to_cout():
    source = (ROOT / 'worker.cpp').read_text()
    main = source[source.index('int wmain'):]
    assert main.index('native_protocol::Output protocol') < main.index('OrtGetApiBase()->GetApi')
    assert 'protocol.capabilities(capabilities.dump())' in main
    assert 'protocol.frame(out)' in main
    assert 'std::cout' not in main
    header = (ROOT / 'protocol_output.hpp').read_text()
    assert 'F_DUPFD_CLOEXEC' in header and 'HANDLE_FLAG_INHERIT, 0' in header
    assert 'SetStdHandle(STD_OUTPUT_HANDLE' in header
    assert 'errno == EINTR' in header
    assert 'max_frame_bytes = 16 * 1024 * 1024' in header
    assert '~Output() { close(); }' in header
    assert "'protocol_output_sha256': sha256(ROOT / 'protocol_output.hpp')" in (ROOT / 'build.py').read_text()


@pytest.mark.parametrize('mode', ['capabilities', 'frames', 'bounds'])
def test_actual_stdout_noise_cannot_enter_protocol(mode):
    value = os.environ.get('VSE_NATIVE_PROTOCOL_TEST_BINARY')
    if not value:
        pytest.skip('set VSE_NATIVE_PROTOCOL_TEST_BINARY to the standalone C++ test target')
    binary = Path(value).resolve(strict=True)
    result = subprocess.run([str(binary), mode], capture_output=True, timeout=30, check=True)
    for marker in (b'printf-main-noise', b'cout-main-noise', b'raw-main-noise',
                   b'background-noise', b'after-destruction-noise'):
        assert marker in result.stderr
        assert marker not in result.stdout
    if mode == 'capabilities':
        assert json.loads(result.stdout) == {'available': True, 'unicode': '\u5b57'}
        return
    frames, data = [], result.stdout
    while data:
        assert len(data) >= 4
        length, = struct.unpack('<I', data[:4])
        assert 0 < length <= 16 * 1024 * 1024
        assert len(data) >= 4 + length
        frames.append(json.loads(data[4:4 + length]))
        data = data[4 + length:]
    if mode == 'bounds':
        assert frames == [{'rejected': 3}]
    else:
        assert result.stdout[:4] == b'\x0a\x01\x00\x00'
        assert frames == [{'data': 'x' * 255}, {'data': 'y' * (2 * 1024 * 1024)}, {'after': True}]
