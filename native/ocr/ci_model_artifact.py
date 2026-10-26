#!/usr/bin/env python3
"""Offline, fixed-anchor import of a legacy 31-file GitHub model artifact.

For source diagnostics only, never final-package acceptance or new conversion
provenance. Official API metadata must be fetched separately by the workflow;
these unsigned local records are not cryptographic proof of GitHub origin.
Committed model/config pins, not metadata or a cached manifest, authorize bytes.
No network, tokens, model conversion, installation, or cache writes are used.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import sys
import tempfile
import zipfile

import verify_model_cache
from prepare_models import load_pins

ROOT = Path(__file__).resolve().parent
CHUNK = 1024 * 1024
MAX_METADATA = 1024 * 1024
PRODUCER_JOB_NAME = 'Native CPU and dual-provider Mac CoreML matrix / Pinned FP32 models / conversion only'
SCOPE = 'Source diagnostic byte reuse only; not conversion provenance, signed attestation, final-package acceptance, or GPU verification.'


def _json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate JSON key: ' + key)
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=unique)


def _positive(value):
    return type(value) is int and value > 0


def _hex(value, width):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(width) + '}', value) is not None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, getattr(info, 'st_birthtime_ns', None))


def _stamp(info):
    return _identity(info) + (info.st_ctime_ns,)


def _plain(path, *, directory=False):
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlink/reparse input path is forbidden: ' + str(parent))
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Expected regular ' + ('directory' if directory else 'file'))
    return path


@contextmanager
def _opened(path):
    path = _plain(path)
    before = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise ValueError('Input changed while opening')
        yield stream
        finished = os.fstat(stream.fileno())
        after = path.lstat()
        # Windows CPython 3.12 path/fd ctime use different clocks. Compare
        # ctime only within the same API, without dropping either check.
        if (_stamp(opened) != _stamp(finished) or _stamp(before) != _stamp(after)
                or _identity(finished) != _identity(after)):
            raise ValueError('Input changed during verification')


def _metadata(path):
    with _opened(path) as stream:
        data = stream.read(MAX_METADATA + 1)
        if len(data) > MAX_METADATA:
            raise ValueError('Oversized API metadata')
    value = _json(data)
    if not isinstance(value, dict):
        raise ValueError('API metadata must be an object')
    return value, hashlib.sha256(data).hexdigest()


def validate_metadata(artifact, run, job, anchors):
    keys = {'repository', 'run_id', 'run_attempt', 'commit', 'artifact_id', 'artifact_sha256', 'artifact_bytes', 'job_id'}
    if (not isinstance(anchors, dict) or set(anchors) != keys
            or not isinstance(anchors['repository'], str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', anchors['repository'])
            or not _hex(anchors['commit'], 40) or not _hex(anchors['artifact_sha256'], 64)
            or any(not _positive(anchors[k]) for k in ('run_id', 'run_attempt', 'artifact_id', 'artifact_bytes', 'job_id'))):
        raise ValueError('Missing/invalid explicit source anchors')
    expected_artifact = {'id': anchors['artifact_id'], 'name': 'native-ocr-fp32-models-' + anchors['commit'],
                         'size_in_bytes': anchors['artifact_bytes'], 'digest': 'sha256:' + anchors['artifact_sha256'],
                         'expired': False}
    if any(type(artifact.get(k)) is not type(v) or artifact.get(k) != v for k, v in expected_artifact.items()):
        raise ValueError('Artifact metadata differs from fixed source anchors')
    workflow = artifact.get('workflow_run')
    if (not isinstance(workflow, dict) or type(workflow.get('id')) is not int
            or workflow.get('id') != anchors['run_id'] or workflow.get('head_sha') != anchors['commit']):
        raise ValueError('Artifact workflow_run identity mismatch')
    if (any(type(run.get(k)) is not int or run[k] != anchors[source]
            for k, source in (('id', 'run_id'), ('run_attempt', 'run_attempt')))
            or run.get('head_sha') != anchors['commit']
            or not isinstance(run.get('repository'), dict)
            or run['repository'].get('full_name') != anchors['repository']):
        raise ValueError('Run metadata differs from fixed source anchors')
    expected_job = {'id': anchors['job_id'], 'run_id': anchors['run_id'],
                    'head_sha': anchors['commit'],
                    'status': 'completed', 'conclusion': 'success', 'name': PRODUCER_JOB_NAME}
    if any(type(job.get(k)) is not type(v) or job.get(k) != v for k, v in expected_job.items()):
        raise ValueError('Producer job metadata differs from the completed pinned conversion job')
    if 'run_attempt' in job and (type(job['run_attempt']) is not int or job['run_attempt'] != anchors['run_attempt']):
        raise ValueError('Producer job attempt mismatch')
    return dict(anchors)


def _inventory(pins):
    limits = {name: MAX_METADATA for name in pins['config_sha256']}
    limits.update({'models/' + row['name'] + '_infer.onnx': row['onnx_bytes'] for row in pins['models']})
    limits.update({'model-manifest.json': MAX_METADATA, 'model-source-pins.json': MAX_METADATA,
                   'unavailable-models.json': 1024})
    if len(limits) != 31:
        raise ValueError('Expected the reviewed legacy 31-file model contract')
    return limits


def _zip_entries(stream, size, limits):
    # Bound central-directory allocation before zipfile reads it. This legacy
    # artifact is < 512 MiB with <= 33 entries; ZIP64 is unnecessary and rejected.
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    position = tail.rfind(b'PK\x05\x06')
    if position < 0 or position + 22 > len(tail):
        raise ValueError('Missing ZIP end record')
    _, disk, central_disk, disk_count, count, central_size, central_offset, comment = struct.unpack_from('<4s4H2IH', tail, position)
    end_offset = size - len(tail) + position
    if (disk or central_disk or disk_count != count or not 31 <= count <= 33
            or comment or position + 22 != len(tail) or central_size > 128 * 1024
            or central_offset + central_size != end_offset):
        raise ValueError('Invalid/bounded single-volume ZIP directory')
    stream.seek(0)
    archive = zipfile.ZipFile(stream)
    entries, names, regions = {}, set(), []
    try:
        if len(archive.infolist()) != count:
            raise ValueError('ZIP entry count differs from end record')
        for entry in archive.infolist():
            name = entry.orig_filename
            if (name != entry.filename or name.casefold() in names
                    or name not in {*limits, 'models/', 'config/'}):
                raise ValueError('Unexpected, duplicate, or unsafe raw ZIP name: ' + repr(name))
            names.add(name.casefold())
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (entry.flag_bits & (1 | 64) or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or mode not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
                    or entry.external_attr & 0x400):
                raise ValueError('Unsafe ZIP entry type/encryption/compression')
            if entry.is_dir():
                if entry.file_size or entry.compress_size:
                    raise ValueError('Directory entry has payload')
            elif (entry.file_size > limits[name] or entry.file_size < 1
                  or name.startswith('models/') and entry.file_size != limits[name]):
                raise ValueError('ZIP entry exceeds fixed model/metadata bounds')
            if entry.header_offset < 0 or entry.header_offset + 30 > central_offset:
                raise ValueError('ZIP local header out of bounds')
            stream.seek(entry.header_offset)
            header = stream.read(30)
            values = struct.unpack('<4s5H3I2H', header)
            signature, _, flags, method = values[:4]
            name_length, extra_length = values[-2:]
            data_offset = entry.header_offset + 30 + name_length + extra_length
            if (signature != b'PK\x03\x04' or flags != entry.flag_bits or method != entry.compress_type
                    or data_offset + entry.compress_size > central_offset):
                raise ValueError('ZIP local header/data bounds mismatch')
            raw_name = stream.read(name_length)
            if raw_name != name.encode('utf-8' if flags & 0x800 else 'cp437'):
                raise ValueError('ZIP local and central names differ')
            regions.append((entry.header_offset, data_offset + entry.compress_size))
            if not entry.is_dir():
                entries[name] = entry
        regions.sort()
        if not regions or regions[0][0] != 0 or any(a[1] > b[0] for a, b in zip(regions, regions[1:])):
            raise ValueError('ZIP payloads overlap or contain a prefix')
        if set(entries) != set(limits):
            raise ValueError('Model artifact file set differs from the 31-file contract')
    except BaseException:
        archive.close()
        raise
    return archive, entries


def _publish(stage, output):
    if os.name == 'nt':
        os.rename(stage, output)  # Windows never replaces an existing directory.
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        function = getattr(library, 'renameat2', None)
        arguments = (-100, os.fsencode(stage), -100, os.fsencode(output), 1)
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform == 'darwin':
        # Apple XNU bsd/sys/stdio.h: RENAME_EXCL=4. No unsafe fallback.
        function = getattr(library, 'renamex_np', None)
        arguments = (os.fsencode(stage), os.fsencode(output), 4)
        types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    else:
        raise RuntimeError('Atomic no-replace publication unsupported on this host')
    if function is None:
        raise RuntimeError('Atomic no-replace publication unavailable')
    function.argtypes, function.restype = types, ctypes.c_int
    if function(*arguments):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(output))


def consume(archive_path, artifact_metadata, run_metadata, job_metadata, *, anchors, consumer_commit, output):
    if not _hex(consumer_commit, 40):
        raise ValueError('Explicit actual consumer commit is required')
    artifact, artifact_metadata_sha = _metadata(artifact_metadata)
    run, run_metadata_sha = _metadata(run_metadata)
    job, job_metadata_sha = _metadata(job_metadata)
    source = validate_metadata(artifact, run, job, anchors)
    pins_path = ROOT / 'model-source-pins.json'
    pins_before, pins_sha = _metadata(pins_path)
    pins = load_pins(pins_path)
    if pins != pins_before:
        raise ValueError('Committed pins changed while loading')
    limits = _inventory(pins)
    if anchors['artifact_bytes'] > sum(limits.values()) + 128 * 1024:
        raise ValueError('Artifact byte count exceeds bounded model contract')
    output = Path(os.path.abspath(output))
    parent = _plain(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Output must be new; never replace an existing directory')
    parent_identity = parent.lstat()
    stage = Path(tempfile.mkdtemp(prefix='.model-artifact-', dir=parent))
    published = False
    try:
        models = stage / 'model-cache'
        models.mkdir()
        inventory = {}
        with _opened(archive_path) as stream:
            if os.fstat(stream.fileno()).st_size != anchors['artifact_bytes']:
                raise ValueError('Archive byte count differs from fixed source anchor')
            if hashlib.file_digest(stream, 'sha256').hexdigest() != anchors['artifact_sha256']:
                raise ValueError('Archive SHA256 differs from fixed source anchor')
            archive, entries = _zip_entries(stream, anchors['artifact_bytes'], limits)
            with archive:
                for name, entry in sorted(entries.items()):
                    destination = models / name
                    destination.parent.mkdir(exist_ok=True)
                    digest, count = hashlib.sha256(), 0
                    with archive.open(entry) as reader, destination.open('xb') as writer:
                        while data := reader.read(CHUNK):
                            count += len(data)
                            if count > limits[name] or count > entry.file_size:
                                raise ValueError('Expanded member exceeds fixed size bound')
                            digest.update(data)
                            writer.write(data)
                    if count != entry.file_size:
                        raise ValueError('Expanded member byte count mismatch')
                    inventory[name] = {'bytes': count, 'sha256': digest.hexdigest()}
            stream.seek(0)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != anchors['artifact_sha256']:
                raise ValueError('Archive changed during extraction')
        # The verifier independently reads this checkout's committed pins.
        rows = verify_model_cache.verify(models, require_provenance=False)
        if _metadata(pins_path)[1] != pins_sha:
            raise ValueError('Committed pins changed during verification')
        receipt = {'schema_version': 1, 'scope': SCOPE, 'source': source,
                   'consumer': {'commit': consumer_commit}, 'model_count': len(rows),
                   'source_format': 'legacy-v1-no-conversion-provenance',
                   'metadata_sha256': {'artifact': artifact_metadata_sha, 'run': run_metadata_sha, 'job': job_metadata_sha},
                   'committed_pins_sha256': pins_sha,
                   'files': inventory, 'gpu_inference_verified': False}
        (stage / 'source-artifact-receipt.json').write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        # Creating our temporary directory changes the parent's mtime/ctime;
        # its device/inode/type must nonetheless remain the original directory.
        final_parent = _plain(parent, directory=True).lstat()
        if (parent_identity.st_dev, parent_identity.st_ino, parent_identity.st_mode) != (final_parent.st_dev, final_parent.st_ino, final_parent.st_mode):
            raise ValueError('Output parent was replaced')
        _publish(stage, output)
        published = True
        return receipt
    finally:
        if not published:
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('archive', 'artifact-metadata', 'run-metadata', 'job-metadata', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('repository', 'source-commit', 'artifact-sha256', 'consumer-commit'):
        parser.add_argument('--' + name, required=True)
    for name in ('source-run-id', 'source-run-attempt', 'source-job-id', 'artifact-id', 'artifact-bytes'):
        parser.add_argument('--' + name, type=int, required=True)
    args = parser.parse_args()
    anchors = {'repository': args.repository, 'commit': args.source_commit, 'run_id': args.source_run_id,
               'run_attempt': args.source_run_attempt, 'job_id': args.source_job_id, 'artifact_id': args.artifact_id,
               'artifact_sha256': args.artifact_sha256, 'artifact_bytes': args.artifact_bytes}
    receipt = consume(args.archive, args.artifact_metadata, args.run_metadata, args.job_metadata, anchors=anchors,
                      consumer_commit=args.consumer_commit, output=args.output)
    print(json.dumps({'result': 'verified-source-diagnostic', 'model_count': receipt['model_count'],
                      'models': str(args.output / 'model-cache'), 'receipt': str(args.output / 'source-artifact-receipt.json')}))


if __name__ == '__main__':
    main()
