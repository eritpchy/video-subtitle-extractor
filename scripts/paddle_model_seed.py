"""Build-only, offline recovery of pinned originals from an accepted CPU ZIP.

The supplied archive, checksum and receipt must come from the explicitly
identified successful source run. A CI receipt is not a signed attestation.
No runtime is extracted, imported or executed; this is byte reuse, not new OCR
inference evidence. Future builds still independently verify the model tree.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile

try:
    from . import paddle_package_models as models
    from . import release_guard as guard
except ImportError:
    import paddle_package_models as models
    import release_guard as guard


FINAL_SCOPE = ('Final ZIP launcher, explicit CPU video modes, regressions and '
               'child-environment isolation; not GPU certification.')
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_ENTRIES = 200000
CHUNK = 1024 * 1024


def _file(path):
    path = Path(path)
    guard._checked_file(path)
    return path.resolve(strict=True)


def _absent(path):
    if path.exists() or path.is_symlink():
        raise FileExistsError('Seed destination must be new; refusing replacement: ' + str(path))


def _publish_directory(source, destination):
    """Atomic no-replace publication, including a concurrent empty directory."""
    if os.name == 'nt':
        os.rename(source, destination)  # Windows refuses existing destinations.
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        rename = getattr(library, 'renameat2', None)
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform == 'darwin':
        # Apple XNU bsd/sys/stdio.h defines RENAME_EXCL = 0x00000004;
        # bsd/man/man2/rename.2 requires EEXIST if the destination exists,
        # and ENOTSUP for filesystems without this flag. Never fall back.
        # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/stdio.h
        # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/man/man2/rename.2
        rename = getattr(library, 'renamex_np', None)
        arguments = (os.fsencode(source), os.fsencode(destination), 0x00000004)
        types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    else:
        raise RuntimeError('Host lacks supported atomic no-replace seed publication')
    if rename is None:
        raise RuntimeError('Host lacks atomic no-replace seed publication')
    rename.argtypes, rename.restype = types, ctypes.c_int
    if rename(*arguments) != 0:
        code = ctypes.get_errno()
        if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
            raise RuntimeError('Filesystem lacks atomic no-replace seed publication')
        raise OSError(code, os.strerror(code), str(destination))


def _read_local(path, bound):
    if path.stat().st_size > bound:
        raise RuntimeError('Oversized seed metadata: ' + path.name)
    with path.open('rb') as stream:
        data = stream.read(bound + 1)
    if len(data) > bound:
        raise RuntimeError('Oversized seed metadata: ' + path.name)
    return data


def _zip_entries(bundle, root):
    entries = bundle.infolist()
    if len(entries) > MAX_ENTRIES:
        raise RuntimeError('Too many source ZIP entries')
    result, files = {}, set()
    for entry in entries:
        name = entry.orig_filename
        path = PurePosixPath(name)
        mode = stat.S_IFMT(entry.external_attr >> 16)
        parts = path.parts
        windows_bad = any(part.endswith((' ', '.')) or re.fullmatch(
            r'(?i:CON|PRN|AUX|NUL|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])(?:\..*)?', part)
            for part in parts)
        if (name != entry.filename or '\x00' in name or '\\' in name
                or any(ord(char) < 32 or ord(char) == 127 or char in ':<>"|?*' for char in name)
                or path.is_absolute() or '..' in parts or str(path) != name.rstrip('/')
                or not name.startswith(root + '/') or windows_bad
                or entry.flag_bits & 1 or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                or (mode == stat.S_IFDIR and not entry.is_dir())
                or (mode == stat.S_IFREG and entry.is_dir())
                or (entry.external_attr & 0x10 and not entry.is_dir())
                or (entry.is_dir() and entry.file_size != 0)):
            raise RuntimeError('Unsafe source ZIP entry: ' + repr(name))
        key = name.rstrip('/').casefold()
        if key in result:
            raise RuntimeError('Duplicate or colliding source ZIP entry: ' + repr(name))
        result[key] = entry
        if not entry.is_dir():
            files.add(key)
    for key in result:
        if any(str(parent) in files for parent in PurePosixPath(key).parents):
            raise RuntimeError('File/directory collision in source ZIP: ' + key)
    return {entry.filename: entry for entry in result.values()}


def _stream_member(bundle, entry, bound, destination=None):
    if entry.is_dir() or entry.file_size < 0 or entry.file_size > bound:
        raise RuntimeError('Oversized or non-file source ZIP member: ' + entry.filename)
    digest, size, chunks = hashlib.sha256(), 0, []
    with bundle.open(entry, 'r') as source:
        while chunk := source.read(min(CHUNK, bound - size + 1)):
            size += len(chunk)
            if size > bound:
                raise RuntimeError('Source ZIP member exceeded its bound: ' + entry.filename)
            digest.update(chunk)
            if destination is None:
                chunks.append(chunk)
            else:
                destination.write(chunk)
    if size != entry.file_size:
        raise RuntimeError('Source ZIP member size mismatch: ' + entry.filename)
    return digest.hexdigest(), size, b''.join(chunks)


def _validate_anchors(source_sha, artifact_run, archive_sha256, receipt_sha256):
    if not isinstance(source_sha, str) or not re.fullmatch(r'[a-f0-9]{40}', source_sha):
        raise ValueError('An exact source commit SHA is required')
    if not isinstance(artifact_run, str) or not re.fullmatch(r'[1-9][0-9]*', artifact_run):
        raise ValueError('An explicit numeric source artifact run is required')
    for name, value in (('archive', archive_sha256), ('receipt', receipt_sha256)):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ValueError('An exact externally trusted ' + name + ' SHA256 is required')


def verify_seed(output: Path, *, source_sha: str, artifact_run: str,
                archive_sha256: str, receipt_sha256: str) -> dict:
    """Recheck cached seed bytes and explicit original-source anchors offline."""
    _validate_anchors(source_sha, artifact_run, archive_sha256, receipt_sha256)
    root = models._plain_path(output)
    if not root.is_dir() or {path.name for path in root.iterdir()} != {'models', 'provenance.json'}:
        raise RuntimeError('Seed container must contain only models and provenance.json')
    provenance_path = _file(root / 'provenance.json')
    data = _read_local(provenance_path, MAX_METADATA_BYTES)
    provenance = guard._read_json(data, provenance_path.name)
    required = {'schema': 1, 'result': 'verified', 'scope': 'pinned-original-model-seed',
                'source_sha': source_sha, 'artifact_run': artifact_run,
                'source_platform': 'windows', 'source_arch': 'x64', 'source_build_profile': 'paddle-cpu',
                'archive_sha256': archive_sha256, 'receipt_sha256': receipt_sha256,
                'source_pins_sha256': models.PINNED_SOURCE_SHA256, 'model_count': 15,
                'source_acceptance_verified': True, 'runtime_included': False,
                'inference_executed': False, 'gpu_inference_verified': False,
                'provenance_limit': 'Caller-supplied source run; local CI receipt, not a signed attestation.'}
    for key, value in required.items():
        if provenance.get(key) != value or type(provenance.get(key)) is not type(value):
            raise RuntimeError('Seed provenance does not match the trusted source: ' + key)
    variable = {'source_version', 'archive', 'archive_bytes', 'checksum_sha256', 'manifest_sha256',
                'source_model_evidence', 'model_inventory_sha256'}
    if set(provenance) != set(required) | variable or models._canonical(provenance) != data:
        raise RuntimeError('Unexpected or noncanonical seed provenance')
    version = provenance['source_version']
    if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', version)
            or provenance['archive'] != guard.archive_name(version, 'windows', 'x64', 'paddle-cpu')
            or type(provenance['archive_bytes']) is not int or provenance['archive_bytes'] <= 0):
        raise RuntimeError('Invalid seed archive identity')
    for key in ('checksum_sha256', 'manifest_sha256', 'model_inventory_sha256'):
        if not isinstance(provenance[key], str) or not re.fullmatch(r'[a-f0-9]{64}', provenance[key]):
            raise RuntimeError('Invalid seed provenance hash: ' + key)
    evidence = provenance['source_model_evidence']
    if (not isinstance(evidence, dict) or set(evidence) != {'bytes', 'sha256'}
            or not isinstance(evidence['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', evidence['sha256'])
            or type(evidence['bytes']) is not int or evidence['bytes'] <= 0):
        raise RuntimeError('Invalid source model evidence identity')
    report = models.verify_models(root / 'models')
    if (report['source_pins_sha256'] != provenance['source_pins_sha256']
            or models._digest(root / 'models' / models.INVENTORY_NAME) != provenance['model_inventory_sha256']):
        raise RuntimeError('Seed provenance does not match original model inventory/pins')
    return provenance


def seed_models(archive: Path, checksum: Path, acceptance: Path, output: Path,
                *, source_sha: str, artifact_run: str,
                archive_sha256: str, receipt_sha256: str) -> dict:
    """Atomically create output/models and provenance from one accepted archive.

    source_sha identifies the original accepted build, never the consuming
    checkout's later revision. artifact_run is caller-supplied source lineage;
    this offline function does not claim to query or authenticate a remote run.
    """
    _validate_anchors(source_sha, artifact_run, archive_sha256, receipt_sha256)
    archive, checksum, acceptance = map(_file, (archive, checksum, acceptance))
    output = Path(os.path.abspath(output))
    _absent(output)
    output = output.resolve()
    if any(source == output or output in source.parents for source in (archive, checksum, acceptance)):
        raise ValueError('Seed destination overlaps source assets')
    checksum_data = _read_local(checksum, 4096)
    acceptance_data = _read_local(acceptance, 1024 * 1024)
    if hashlib.sha256(acceptance_data).hexdigest() != receipt_sha256:
        raise RuntimeError('Source receipt differs from the externally trusted SHA256')
    receipt = guard._read_json(acceptance_data, acceptance.name)
    archive_digest = guard.sha256(archive)  # Streaming: final bundles can be large.
    if archive_digest != archive_sha256:
        raise RuntimeError('Source archive differs from the externally trusted SHA256')
    if checksum_data.decode('utf-8').strip() != f'{archive_digest}  {archive.name}':
        raise RuntimeError('Source archive checksum/name mismatch')
    pin_bytes, pins = models._read_pins(models.BUILD_PINS)
    expected = {models.PINS_NAME: (models.PINNED_SOURCE_SHA256, MAX_METADATA_BYTES),
                models.INVENTORY_NAME: (None, MAX_METADATA_BYTES)}
    for row in pins['models']:
        folder = models._model_path(row['name'])
        for name, digest in row['files'].items():
            expected[folder + '/' + name] = (digest, row['archive_bytes'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.paddle-seed-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'seed'
        model_root = stage / 'models'
        model_root.mkdir(parents=True)
        with zipfile.ZipFile(archive) as bundle:
            entries = _zip_entries(bundle, archive.stem)
            manifest_name = archive.stem + '/bundle-manifest.json'
            manifests = [name for name in entries if name.endswith('/bundle-manifest.json')]
            if manifests != [manifest_name]:
                raise RuntimeError('Missing unique source bundle manifest')
            manifest_digest, _, data = _stream_member(bundle, entries[manifest_name], MAX_MANIFEST_BYTES)
            manifest = guard._read_json(data, manifest_name)
            identity = {'commit': source_sha, 'platform': 'windows', 'arch': 'x64',
                        'build_profile': 'paddle-cpu', 'ocr_backend': 'paddle', 'accelerator': 'cpu',
                        'format': 2, 'python': '3.12', 'gpu_inference_verified': False}
            if any(manifest.get(key) != value or type(manifest.get(key)) is not type(value)
                   for key, value in identity.items()):
                raise RuntimeError('Source is not the specified Windows Paddle CPU final bundle')
            version = manifest.get('version')
            if (not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', version)
                    or archive.name != guard.archive_name(version, 'windows', 'x64', 'paddle-cpu')):
                raise RuntimeError('Source bundle filename/version mismatch')
            guard.validate_acceptance_receipt(acceptance, archive, archive_digest, manifest)
            if receipt.get('scope') != FINAL_SCOPE:
                raise RuntimeError('Source receipt does not have final ZIP acceptance scope')
            report_key = f'package/test-results/{archive.stem}/paddle-models/report.json'
            evidence = receipt.get('evidence')
            model_evidence = evidence.get(report_key) if isinstance(evidence, dict) else None
            if (not isinstance(model_evidence, dict)
                    or not isinstance(model_evidence.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', model_evidence['sha256'])
                    or type(model_evidence.get('bytes')) is not int or model_evidence['bytes'] <= 0):
                raise RuntimeError('Missing hash-bound final model evidence in source receipt')
            prefix = archive.stem + '/backend/models/'
            allowed_dirs = {''} | {str(parent) for name in expected for parent in PurePosixPath(name).parents
                                   if str(parent) != '.'}
            selected = {}
            for name, entry in entries.items():
                if name.startswith(prefix):
                    relative = name[len(prefix):]
                    if entry.is_dir():
                        if relative.rstrip('/') not in allowed_dirs:
                            raise RuntimeError('Unexpected model directory in source ZIP')
                    else:
                        selected[relative] = entry
            if set(selected) != set(expected):
                raise RuntimeError('Source ZIP does not contain exactly 45 model files plus pins/inventory')
            for row in pins['models']:
                folder = models._model_path(row['name'])
                if sum(selected[folder + '/' + name].file_size for name in row['files']) > row['archive_bytes']:
                    raise RuntimeError('Oversized source model relative to its pinned original archive')
            manifest_files = manifest.get('files')
            if not isinstance(manifest_files, dict):
                raise RuntimeError('Missing source payload file hashes')
            if {name for name in manifest_files if name.startswith('backend/models/')} != {
                    'backend/models/' + name for name in expected}:
                raise RuntimeError('Source manifest model file set mismatch')
            for name, (pinned_digest, bound) in expected.items():
                destination = model_root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open('xb') as stream:
                    digest, _, _ = _stream_member(bundle, selected[name], bound, stream)
                if digest != manifest_files.get('backend/models/' + name):
                    raise RuntimeError('Source manifest model hash mismatch: ' + name)
                if pinned_digest is not None and digest != pinned_digest:
                    raise RuntimeError('Fixed original model hash mismatch: ' + name)
        report = models.verify_models(model_root)
        if ((model_root / models.PINS_NAME).read_bytes() != pin_bytes
                or manifest.get('paddle_models') != report):
            raise RuntimeError('Source model inventory does not match independently verified originals')
        provenance = {'schema': 1, 'result': 'verified', 'scope': 'pinned-original-model-seed',
                      'source_sha': source_sha, 'artifact_run': artifact_run, 'source_version': version,
                      'source_platform': 'windows', 'source_arch': 'x64', 'source_build_profile': 'paddle-cpu',
                      'archive': archive.name, 'archive_sha256': archive_digest,
                      'archive_bytes': archive.stat().st_size,
                      'receipt_sha256': hashlib.sha256(acceptance_data).hexdigest(),
                      'checksum_sha256': hashlib.sha256(checksum_data).hexdigest(),
                      'manifest_sha256': manifest_digest, 'source_model_evidence': model_evidence,
                      'model_inventory_sha256': models._digest(model_root / models.INVENTORY_NAME),
                      'source_pins_sha256': models.PINNED_SOURCE_SHA256, 'model_count': 15,
                      'source_acceptance_verified': True, 'runtime_included': False,
                      'inference_executed': False, 'gpu_inference_verified': False,
                      'provenance_limit': 'Caller-supplied source run; local CI receipt, not a signed attestation.'}
        (stage / 'provenance.json').write_bytes(models._canonical(provenance))
        verify_seed(stage, source_sha=source_sha, artifact_run=artifact_run,
                    archive_sha256=archive_sha256, receipt_sha256=receipt_sha256)
        # Do not publish bytes read while an input or its sidecar was changing.
        for source in (archive, checksum, acceptance):
            _file(source)
        if (guard.sha256(archive) != archive_digest or _read_local(checksum, 4096) != checksum_data
                or _read_local(acceptance, 1024 * 1024) != acceptance_data):
            raise RuntimeError('Source assets changed during seed creation')
        _absent(output)
        _publish_directory(stage, output)
    return provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--archive', type=Path)
    action.add_argument('--verify-seed', type=Path)
    for name in ('checksum', 'acceptance', 'output'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--artifact-run', required=True)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--receipt-sha256', required=True)
    args = parser.parse_args(argv)
    anchors = dict(source_sha=args.source_sha, artifact_run=args.artifact_run,
                   archive_sha256=args.archive_sha256, receipt_sha256=args.receipt_sha256)
    if args.verify_seed is not None:
        if any(value is not None for value in (args.checksum, args.acceptance, args.output)):
            parser.error('--verify-seed takes no checksum, acceptance or output options')
        report = verify_seed(args.verify_seed, **anchors)
    else:
        if any(value is None for value in (args.checksum, args.acceptance, args.output)):
            parser.error('--archive requires --checksum, --acceptance and --output')
        report = seed_models(args.archive, args.checksum, args.acceptance, args.output, **anchors)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise SystemExit('Paddle model seed blocked: ' + str(error))
