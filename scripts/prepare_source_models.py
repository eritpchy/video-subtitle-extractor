#!/usr/bin/env python3
"""Prepare complete, pinned model DATA for Python source backends, stdlib only.

Paddle sources come from their 15 pinned official archives. ONNX data comes
from one hash-pinned public release ZIP; its worker, runtime and executable
code are neither extracted nor executed. This is data reuse, not conversion,
inference validation, an Actions artifact, or a claim about native binaries.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import sys
import tempfile
import urllib.request
import zipfile

try:
    from . import paddle_package_models as paddle
except ImportError:
    import paddle_package_models as paddle

ARCHIVE_URL = ('https://github.com/eritpchy/video-subtitle-extractor/releases/download/'
               '2.3.0/vse-2.3.0-linux-x64-native-cpu.zip')
ARCHIVE_SHA256 = '60e6f8098c058777273b5a6820d804731b5c93c45c78b14f5d8d1d1165361a11'
ARCHIVE_BYTES = 517238092
MODEL_PREFIX = 'vse-2.3.0-linux-x64-native-cpu/native-ocr/'
MAX_METADATA = 1024 * 1024
CHUNK = 1024 * 1024
DOWNLOAD_HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'})


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


def _path(path):
    """Reject links/reparse points, including existing ancestors of new paths."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlink/reparse model path is forbidden: ' + str(part))
    return path


def _absent(path):
    _path(path)
    if path.exists():
        raise FileExistsError('Model output must be NEW; refusing replacement: ' + str(path))


def _publish(stage, output):
    """Atomic no-replace publication, including concurrent empty directories."""
    _absent(output)
    if os.name == 'nt':
        os.rename(stage, output)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        function = getattr(library, 'renameat2', None)
        values = (-100, os.fsencode(stage), -100, os.fsencode(output), 1)
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform == 'darwin':
        function = getattr(library, 'renamex_np', None)
        values = (os.fsencode(stage), os.fsencode(output), 4)  # RENAME_EXCL
        types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    else:
        raise RuntimeError('Atomic no-replace model publication unsupported on this host')
    if function is None:
        raise RuntimeError('Atomic no-replace model publication unavailable')
    function.argtypes, function.restype = types, ctypes.c_int
    if function(*values):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(output))


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _archive_stream(path):
    path = _path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size != ARCHIVE_BYTES:
        raise ValueError('Public model ZIP size/type differs from the fixed release')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        # Compare ctime within each API: Windows path/fd clocks differ.
        if _stamp(opened)[:-1] != _stamp(before)[:-1]:
            raise ValueError('Model ZIP changed while opening')
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Public model ZIP SHA256 differs from the fixed release')
        stream.seek(0)
        yield stream
        stream.seek(0)
        if hashlib.file_digest(stream, 'sha256').hexdigest() != ARCHIVE_SHA256:
            raise ValueError('Public model ZIP changed during extraction')
        if _stamp(opened) != _stamp(os.fstat(stream.fileno())) or _stamp(before) != _stamp(path.lstat()):
            raise ValueError('Public model ZIP changed during verification')


def _check_download_url(url):
    from urllib.parse import urlsplit
    value = urlsplit(url)
    if (value.scheme != 'https' or value.hostname not in DOWNLOAD_HOSTS
            or value.port not in (None, 443) or value.username or value.password):
        raise ValueError('Unexpected public model download redirect; no alternative source is allowed')


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _check_download_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(cache, offline):
    cache = _path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / (ARCHIVE_SHA256 + '.zip')
    if destination.exists() or destination.is_symlink():
        with _archive_stream(destination):
            pass
        return destination
    if offline:
        raise FileNotFoundError('Offline public model ZIP missing: ' + str(destination))
    _check_download_url(ARCHIVE_URL)
    opener = urllib.request.build_opener(_PublicRedirect())
    temporary = None
    try:
        with opener.open(ARCHIVE_URL, timeout=120) as response:
            _check_download_url(response.geturl())
            with tempfile.NamedTemporaryFile(dir=cache, prefix='.model-download-', delete=False) as writer:
                temporary = Path(writer.name)
                total = 0
                while block := response.read(CHUNK):
                    total += len(block)
                    if total > ARCHIVE_BYTES:
                        raise ValueError('Public model download exceeds the fixed byte count')
                    writer.write(block)
        with _archive_stream(temporary):
            pass
        # A concurrent cache winner must be reverified, never overwritten.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            with _archive_stream(destination):
                pass
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _expected(pins):
    files = {'model-source-pins.json': 65536, 'model-manifest.json': MAX_METADATA}
    files.update({name: MAX_METADATA for name in pins['config_sha256']})
    files.update({'models/' + row['name'] + '_infer.onnx': row['onnx_bytes'] for row in pins['models']})
    if len(files) != 30 or len(pins['config_sha256']) != 13:
        raise ValueError('Expected exactly 15 ONNX graphs and 13 JSON configs')
    return files


def verify_onnx(root):
    root = _path(root)
    pin_bytes, pins = paddle._read_pins(paddle.BUILD_PINS)
    limits = _expected(pins)
    actual = set()
    for path in root.rglob('*'):
        _path(path)
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            if relative not in {'models', 'config'}:
                raise ValueError('Unexpected ONNX data directory: ' + relative)
        elif path.is_file():
            if relative not in limits or path.stat().st_size > limits[relative]:
                raise ValueError('Unexpected/oversized ONNX data file: ' + relative)
            actual.add(relative)
        else:
            raise ValueError('Non-regular ONNX data file: ' + relative)
    if actual != set(limits):
        raise ValueError('ONNX source data must contain exactly the complete 30-file set')
    if (root / 'model-source-pins.json').read_bytes() != pin_bytes:
        raise ValueError('ONNX source pins differ from the fixed repository trust root')
    rows = _json((root / 'model-manifest.json').read_bytes())
    expected_rows = []
    inventory = {}
    for row in pins['models']:
        name = row['name']
        config = f'config/{name.split("_")[1]}.json' if name.startswith('PP-OCRv6_') else f'config/{name}.json'
        expected_rows.append({'name': name + '_infer', 'rec_model': name if name.endswith('_rec') else None,
                              'onnx_sha256': row['onnx_sha256'], 'onnx_bytes': row['onnx_bytes'],
                              'source_url': row['url'], 'source_archive_sha256': row['archive_sha256'],
                              'source': {k: {'sha256': v} for k, v in row['files'].items()},
                              'languages': row.get('languages', []), 'converter': pins['converter'],
                              'config_file': config, 'config_sha256': pins['config_sha256'][config]})
        model = 'models/' + name + '_infer.onnx'
        if (root / model).stat().st_size != row['onnx_bytes']:
            raise ValueError('ONNX model size mismatch: ' + name)
        for relative, expected_hash in ((model, row['onnx_sha256']), (config, pins['config_sha256'][config])):
            if _digest(root / relative) != expected_hash:
                raise ValueError('ONNX model/config SHA256 mismatch: ' + relative)
            inventory[relative] = expected_hash
    if rows != expected_rows:
        raise ValueError('ONNX manifest/source/config/language identity differs from fixed pins')
    return {'format': 'onnx', 'model_count': 15, 'config_count': 13, 'data_files': 30,
            'source_pins_sha256': paddle.PINNED_SOURCE_SHA256,
            'model_manifest_sha256': _digest(root / 'model-manifest.json'),
            'files': inventory, 'inference_verified': False, 'binaries_executed': False}


def _entries(stream, limits):
    # Bound central-directory allocation; the fixed 517 MB archive needs no ZIP64.
    stream.seek(max(0, ARCHIVE_BYTES - 65557))
    tail = stream.read(65557)
    end = tail.rfind(b'PK\x05\x06')
    if end < 0 or end + 22 != len(tail):
        raise ValueError('Invalid bounded model ZIP end record')
    _, disk, central_disk, disk_count, count, size, offset, comment = struct.unpack_from('<4s4H2IH', tail, end)
    if (disk or central_disk or disk_count != count or count > 50000 or comment
            or size > 16 * 1024 * 1024 or offset + size != ARCHIVE_BYTES - len(tail) + end):
        raise ValueError('Model ZIP central directory exceeds reviewed bounds')
    archive = zipfile.ZipFile(stream)
    selected, names, model_names = {}, set(), set()
    try:
        for entry in archive.infolist():
            name = entry.orig_filename
            path = PurePosixPath(name)
            if (name != entry.filename or '\x00' in name or '\\' in name or path.is_absolute()
                    or '..' in path.parts or str(path) != name.rstrip('/') or name in names):
                raise ValueError('Unsafe/duplicate raw public ZIP name')
            names.add(name)
            # Linux terminfo/runtime files legitimately differ only by case.
            # They are not extracted; case collisions are forbidden for our
            # cross-platform data subtree, not for unrelated binary payload.
            if name.casefold().startswith(MODEL_PREFIX.casefold()):
                if not name.startswith(MODEL_PREFIX) or name.casefold() in model_names:
                    raise ValueError('Case-ambiguous model data ZIP name')
                model_names.add(name.casefold())
            if not name.startswith(MODEL_PREFIX):
                continue
            relative = name[len(MODEL_PREFIX):]
            if relative not in limits:
                if relative.startswith(('models/', 'config/')) and relative not in ('models/', 'config/'):
                    raise ValueError('Unreviewed graph/config in public model ZIP')
                continue  # Never extract worker, libraries, or other release files.
            if (entry.flag_bits & (1 | 64) or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG)
                    or entry.external_attr & 0x400 or not 0 < entry.file_size <= limits[relative]):
                raise ValueError('Unsafe/non-data model ZIP entry')
            selected[relative] = entry
        if set(selected) != set(limits):
            raise ValueError('Public ZIP does not contain all 30 pinned model data files')
        return archive, selected
    except BaseException:
        archive.close()
        raise


def prepare_onnx(output, *, cache=None, archive=None, offline=False):
    output = _path(output)
    _absent(output)
    if cache is not None:
        cache = _path(cache)
        if output == cache or output in cache.parents or cache in output.parents:
            raise ValueError('Model output and download cache must not overlap')
    if archive is None:
        if cache is None:
            raise ValueError('ONNX preparation requires --cache or an existing --archive')
        archive = _download(cache, offline)
    archive = _path(archive)
    if output == archive or output in archive.parents:
        raise ValueError('Model output and original ZIP must not overlap')
    _, pins = paddle._read_pins(paddle.BUILD_PINS)
    limits = _expected(pins)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-onnx-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'data'
        stage.mkdir()
        with _archive_stream(archive) as stream:
            bundle, selected = _entries(stream, limits)
            with bundle:
                for relative, entry in sorted(selected.items()):
                    destination = stage / relative
                    destination.parent.mkdir(exist_ok=True)
                    count = 0
                    with bundle.open(entry) as reader, destination.open('xb') as writer:
                        while block := reader.read(CHUNK):
                            count += len(block)
                            if count > entry.file_size or count > limits[relative]:
                                raise ValueError('Expanded model data exceeds fixed bounds')
                            writer.write(block)
                    if count != entry.file_size:
                        raise ValueError('Expanded model data size mismatch')
        report = verify_onnx(stage)
        _publish(stage, output)
    report['data_source'] = {'url': ARCHIVE_URL, 'sha256': ARCHIVE_SHA256, 'bytes': ARCHIVE_BYTES,
                             'scope': 'Public release model DATA only; not its binary/runtime or a new conversion.'}
    return report


def prepare_paddle(output, *, cache, offline=False):
    output, cache = _path(output), _path(cache)
    _absent(output)
    if output == cache or output in cache.parents or cache in output.parents:
        raise ValueError('Model output and archive cache must not overlap')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-paddle-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'data'
        report = paddle.prepare_models(stage, cache, offline)
        _publish(stage, output)
    return report


def main(argv=None):
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('The supported source environment is Python 3.12 (not 3.12+)')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('paddle', 'onnx'):
        command = commands.add_parser(name)
        command.add_argument('--output', type=Path, required=True)
        command.add_argument('--cache', type=Path, required=name == 'paddle')
        command.add_argument('--offline', action='store_true')
        if name == 'onnx':
            command.add_argument('--archive', type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--format', choices=('paddle', 'onnx'), required=True)
    verify.add_argument('models', type=Path)
    args = parser.parse_args(argv)
    if args.action == 'paddle':
        report = prepare_paddle(args.output, cache=args.cache, offline=args.offline)
    elif args.action == 'onnx':
        report = prepare_onnx(args.output, cache=args.cache, archive=args.archive, offline=args.offline)
    else:
        report = paddle.verify_models(args.models) if args.format == 'paddle' else verify_onnx(args.models)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
