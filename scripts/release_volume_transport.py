"""Build-side byte volumes for an already accepted Windows release ZIP.

This is not a customer installer, compression, publication authorization, or
Windows restore certification. Source ZIP bytes and acceptance evidence stay
unchanged. No subprocess, network, or extraction is used.

This BUILD-SIDE tool requires the existing ``packaging`` build dependency:
the shared release guard imports it through package_release/runtime_dependencies.
Byte streaming, JSON and ZIP handling use the standard library, but importing
this tool is NOT stdlib-only. It deliberately reuses the authoritative receipt
guard rather than copying or weakening its checks. The customer native EXE
does not require Python or packaging; rebuilding its already-generated fixed
header is a separate workflow and does not import this module.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
import zipfile

try:
    from .release_guard import validate_acceptance_receipt
    from .release_profiles import CANDIDATE_TARGETS, PROFILES, archive_name
except ImportError:
    from release_guard import validate_acceptance_receipt
    from release_profiles import CANDIDATE_TARGETS, PROFILES, archive_name


DEFAULT_PART_BYTES = 2_000_000_000
ASSET_LIMIT = 2 ** 31  # GitHub requires each individual asset to be UNDER 2 GiB.
CHUNK_BYTES = 1024 * 1024
MAX_PARTS = 999
MAX_METADATA_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ENTRIES = 200_000
MAX_EXPANDED_BYTES = 1024 ** 4
RESERVE_BYTES = 64 * 1024 * 1024
IDENTITY_KEYS = ('commit', 'version', 'platform', 'arch', 'build_profile',
                 'ocr_backend', 'accelerator', 'format', 'python')
FORMAT = 'vse-zip-byte-volumes'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
# COM0/LPT0 are conservatively rejected too, matching the native restorer's
# shared filename contract rather than accepting a build-only exception.
_DEVICE = re.compile(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])(?:\..*)?\Z', re.I)


def _error(message):
    raise RuntimeError(message)


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        _error(f'Unexpected {label} fields')


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        _error(f'Invalid {label}')
    return value


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        _error('Invalid SHA-256')
    return value


def _name(value):
    if (not isinstance(value, str) or not value or value in ('.', '..')
            or any(ord(c) < 32 or c in '<>:"/\\|?*~' or 0xd800 <= ord(c) <= 0xdfff for c in value)
            or len(value.encode('utf-16-le')) > 510
            or value.endswith((' ', '.')) or _DEVICE.fullmatch(value)
            or _DEVICE.fullmatch(value.split('.')[0].rstrip(' '))):
        _error(f'Unsafe Windows name: {value!r}')
    return value


def _plain(path: Path, *, directory=False):
    path = Path(path)
    if '..' in path.parts:
        _error(f'Parent traversal is not allowed: {path}')
    path = path.absolute()
    for current in reversed((path, *path.parents)):
        try:
            info = current.lstat()
        except OSError as error:
            raise RuntimeError(f'Missing transport path: {current}') from error
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _error(f'Symlink/junction/reparse path is not accepted: {current}')
        is_directory = current != path or directory
        if not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            _error(f'Unexpected transport file type: {current}')
    return path


@contextmanager
def _open(path):
    path = _plain(path)
    before = path.stat()
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if not stat.S_ISREG(actual.st_mode) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            _error(f'Transport file changed while opening: {path}')
        yield stream
        after = _plain(path).stat()
        if (actual.st_dev, actual.st_ino, actual.st_size, actual.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _error(f'Transport file changed while reading: {path}')


def _read(path, limit):
    with _open(path) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            _error(f'Oversized transport metadata: {path}')
        return stream.read(limit + 1)


def _digest(path):
    digest = hashlib.sha256()
    size = 0
    with _open(path) as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b''):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


def _snapshot(path):
    info = _plain(path).stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error(f'Duplicate JSON field: {key}')
        result[key] = value
    return result


def _json(data):
    try:
        result = json.loads(data, object_pairs_hook=_pairs,
                            parse_constant=lambda value: _error(f'Invalid JSON number: {value}'))
    except (ValueError, UnicodeError) as error:
        raise RuntimeError('Invalid transport JSON') from error
    if not isinstance(result, dict):
        _error('Expected transport JSON object')
    return result


def _identity(value, name):
    _keys(value, IDENTITY_KEYS, 'archive identity')
    if (not isinstance(value['commit'], str) or not re.fullmatch('[0-9a-f]{40}', value['commit'])
            or not isinstance(value['version'], str)
            or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', value['version'])
            or value['platform'] != 'windows' or value['arch'] != 'x64'
            or type(value['format']) is not int or value['format'] != 2 or value['python'] != '3.12'):
        _error('Unsupported transport archive identity/runtime')
    target = (value['platform'], value['arch'], value['build_profile'])
    if any(not isinstance(part, str) for part in target) or target not in CANDIDATE_TARGETS:
        _error('Unknown transport build profile')
    profile = PROFILES[value['build_profile']]
    if (value['ocr_backend'] != profile['backend'] or value['accelerator'] != profile['accelerator']
            or name != archive_name(value['version'], *target)):
        _error('Archive name/backend/profile identity mismatch')


class _ZipReader:
    """Bound any individual ZIP metadata allocation; never read a whole volume."""
    def __init__(self, size):
        self.size, self.position = size, 0

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset + (0 if whence == 0 else self.position if whence == 1 else self.size if whence == 2 else -self.size * 2)
        if not 0 <= position <= self.size:
            raise ValueError('ZIP seek outside the original archive')
        self.position = position
        return position

    def seekable(self):
        return True

    def read(self, size=-1):
        size = self.size - self.position if size is None or size < 0 else min(size, self.size - self.position)
        if size > MAX_METADATA_BYTES:
            _error('ZIP metadata read exceeds the bounded-memory limit')
        result = self.read_at(self.position, size)
        self.position += len(result)
        return result


class _SingleZip(_ZipReader):
    def __init__(self, stream, size):
        super().__init__(size)
        self.stream = stream

    def read_at(self, offset, size):
        self.stream.seek(offset)
        return self.stream.read(size)


class _VolumeZip(_ZipReader):
    def __init__(self, directory, parts, size):
        super().__init__(size)
        self.directory, self.parts = directory, parts
        self.offsets = [part['offset'] for part in parts]
        self._part_index, self._context, self._stream = None, None, None

    def close(self):
        context = self._context
        self._part_index, self._context, self._stream = None, None, None
        if context is not None:
            context.__exit__(None, None, None)

    def read_at(self, offset, size):
        result = bytearray()
        while size:
            index = bisect_right(self.offsets, offset) - 1
            part = self.parts[index]
            local = offset - part['offset']
            count = min(size, part['size'] - local)
            if count <= 0:
                _error('Invalid volume boundary')
            if self._part_index != index:
                self.close()
                self._context = _open(self.directory / part['name'])
                self._stream = self._context.__enter__()
                self._part_index = index
            self._stream.seek(local)
            block = self._stream.read(count)
            if len(block) != count:
                _error('Truncated transport volume')
            result.extend(block)
            offset += count
            size -= count
        return bytes(result)


def _zip_directory(reader):
    """Bound the central directory before zipfile allocates its entry objects."""
    tail_size = min(reader.size, 65535 + 22)
    reader.seek(-tail_size, 2)
    tail = reader.read(tail_size)
    at = tail.rfind(b'PK\x05\x06')
    if at < 0 or len(tail) - at < 22:
        _error('Missing ZIP end directory')
    _, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment = struct.unpack('<4s4H2LH', tail[at:at + 22])
    if at + 22 + comment != len(tail) or disk or cd_disk or count_disk != count:
        _error('Unsupported ZIP trailing data or split-disk directory')
    end = reader.size - tail_size + at
    boundary = end
    locator = b''
    if end >= 20:
        reader.seek(end - 20)
        locator = reader.read(20)
    if locator.startswith(b'PK\x06\x07'):
        _, zip64_disk, offset64, disks = struct.unpack('<4sLQL', locator)
        if zip64_disk or disks != 1 or offset64 + 56 > end - 20:
            _error('Invalid ZIP64 locator')
        reader.seek(offset64)
        record = reader.read(56)
        if len(record) != 56:
            _error('Truncated ZIP64 directory')
        signature, record_size, _, _, disk64, cd_disk64, count_disk64, count64, size64, cd64 = struct.unpack('<4sQ2H2L4Q', record)
        if (signature != b'PK\x06\x06' or record_size < 44 or offset64 + 12 + record_size != end - 20
                or disk64 or cd_disk64 or count_disk64 != count64
                or count not in (count64, 65535) or cd_size not in (size64, 0xffffffff)
                or cd_offset not in (cd64, 0xffffffff)):
            _error('Inconsistent ZIP64 directory')
        count, cd_size, cd_offset, boundary = count64, size64, cd64, offset64
    elif count == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff:
        _error('ZIP64 directory is missing')
    _integer(count, 1, MAX_ENTRIES, 'ZIP entry count')
    _integer(cd_size, 1, MAX_METADATA_BYTES, 'ZIP central directory size')
    if cd_offset + cd_size != boundary:
        _error('Unexpected ZIP central directory boundary')
    # A false small EOCD count must not trick zipfile into allocating millions
    # of entries from a larger central directory. Count the bounded headers
    # without materializing names/comments before constructing ZipInfo objects.
    position = cd_offset
    for _ in range(count):
        reader.seek(position)
        header = reader.read(46)
        if len(header) != 46 or not header.startswith(b'PK\x01\x02'):
            _error('Invalid ZIP central directory entry header')
        name_size, extra_size, comment_size = struct.unpack_from('<3H', header, 28)
        position += 46 + name_size + extra_size + comment_size
        if position > boundary:
            _error('ZIP central directory entry crosses its boundary')
    if position != boundary:
        _error('ZIP central directory entry count/size mismatch')
    return count


def _entry_name(name, root):
    if not isinstance(name, str) or not name.startswith(root + '/') or '\\' in name:
        _error(f'Unsafe ZIP entry name: {name!r}')
    relative = PurePosixPath(name)
    canonical = name[:-1] if name.endswith('/') else name
    if relative.is_absolute() or str(relative) != canonical:
        _error(f'Unsafe ZIP entry path: {name!r}')
    for part in relative.parts:
        _name(part)
    return name.rstrip('/')


def _inspect_zip(reader, name):
    count = _zip_directory(reader)
    root = Path(name).stem
    with zipfile.ZipFile(reader, 'r', allowZip64=True) as archive:
        entries = archive.infolist()
        if len(entries) != count:
            _error('ZIP entry count disagrees with central directory')
        names, rows, expanded = {}, [], 0
        for entry in entries:
            normalized = _entry_name(entry.orig_filename, root)
            mode = stat.S_IFMT(entry.external_attr >> 16)
            directory = entry.is_dir()
            if (entry.orig_filename != entry.filename or '\x00' in entry.orig_filename
                    or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (mode == stat.S_IFDIR and not directory)
                    or entry.external_attr & 0x400 or entry.flag_bits & 1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or (directory and entry.file_size != 0)):
                _error(f'Unsafe/unsupported ZIP entry type: {entry.orig_filename!r}')
            key = unicodedata.normalize('NFC', normalized).casefold()
            if key in names:
                _error(f'Duplicate/case-alias ZIP entry: {normalized}')
            names[key] = directory
            expanded += entry.file_size
            _integer(expanded, 0, MAX_EXPANDED_BYTES, 'ZIP expanded size')
            rows.append({'name': entry.filename, 'kind': 'directory' if directory else 'file', 'size': entry.file_size})
        for key in names:
            if any(names.get(str(parent)) is False for parent in PurePosixPath(key).parents):
                _error('ZIP file/directory prefix conflict')
        manifest_name = root + '/bundle-manifest.json'
        matches = [entry for entry in entries if entry.filename.endswith('/bundle-manifest.json')]
        if len(matches) != 1 or matches[0].filename != manifest_name or matches[0].file_size > 16 * 1024 * 1024:
            _error('ZIP requires one bounded manifest at its expected root')
        raw_manifest = archive.read(matches[0])
        bundle = _json(raw_manifest)
        identity = {key: bundle.get(key) for key in IDENTITY_KEYS}
        _identity(identity, name)
        inventory = bundle.get('files')
        if not isinstance(inventory, dict):
            _error('Bundle file inventory is missing')
        expected = {}
        for relative, digest in inventory.items():
            full_name = root + '/' + relative
            _entry_name(full_name, root)
            expected[full_name] = _hash(digest)
        actual = {row['name'] for row in rows if row['kind'] == 'file'}
        if actual != set(expected) | {manifest_name} or manifest_name in expected:
            _error('ZIP entries do not match the accepted bundle file inventory')
        expected[manifest_name] = hashlib.sha256(raw_manifest).hexdigest()
        for row in rows:
            if row['kind'] == 'file':
                row['sha256'] = expected[row['name']]
    return identity, expanded, rows


def _record(path, data):
    return {'name': path.name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _budget(size, expanded):
    reserve = max(RESERVE_BYTES, (size + expanded + 19) // 20)
    return {'download_bytes': size, 'merged_zip_bytes': size, 'extracted_bytes': expanded,
            'reserve_bytes': reserve, 'required_free_bytes': size + expanded + reserve,
            'total_footprint_bytes': size * 2 + expanded + reserve}


def _validate_document(document, manifest_name):
    _keys(document, ('schema', 'format', 'archive', 'part_bytes', 'parts', 'receipt', 'checksum',
                     'disk', 'gpu_inference_verified', 'client_restore_verified'), 'transport')
    if type(document['schema']) is not int or document['schema'] != 1 or document['format'] != FORMAT:
        _error('Unsupported transport schema/format')
    if document['gpu_inference_verified'] is not False or document['client_restore_verified'] is not False:
        _error('Transport cannot certify GPU inference or a client restore')
    archive = document['archive']
    _keys(archive, ('name', 'size', 'sha256', 'root', 'uncompressed_bytes', 'entry_count', 'identity'), 'archive')
    name = _name(archive['name'])
    if manifest_name != name + '.transport.json' or archive['root'] != Path(name).stem:
        _error('Transport manifest/archive filename mismatch')
    _identity(archive['identity'], name)
    size = _integer(archive['size'], 1, ASSET_LIMIT * MAX_PARTS, 'archive size')
    _hash(archive['sha256'])
    expanded = _integer(archive['uncompressed_bytes'], 0, MAX_EXPANDED_BYTES, 'expanded size')
    _integer(archive['entry_count'], 1, MAX_ENTRIES, 'entry count')
    part_bytes = _integer(document['part_bytes'], 1, ASSET_LIMIT - 1, 'part size (must be under 2 GiB)')
    parts = document['parts']
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS or len(parts) != (size + part_bytes - 1) // part_bytes:
        _error('Invalid volume count')
    expected_names = {manifest_name}
    offset = 0
    for index, part in enumerate(parts, 1):
        _keys(part, ('name', 'index', 'offset', 'size', 'sha256'), 'volume')
        _name(part['name'])
        if (part['name'] != f'{name}.part{index:03d}' or type(part['index']) is not int or part['index'] != index
                or type(part['offset']) is not int or part['offset'] != offset
                or type(part['size']) is not int or part['size'] != min(part_bytes, size - offset)):
            _error('Volume name/order/offset/size mismatch')
        _hash(part['sha256'])
        expected_names.add(part['name'])
        offset += part['size']
    for key, suffix in (('receipt', '.acceptance.json'), ('checksum', '.sha256')):
        record = document[key]
        _keys(record, ('name', 'size', 'sha256'), key)
        _name(record['name'])
        if record['name'] != name + suffix:
            _error('Original acceptance/checksum filename mismatch')
        _integer(record['size'], 1, MAX_MANIFEST_BYTES, key + ' size')
        _hash(record['sha256'])
        expected_names.add(record['name'])
    budget = _budget(size, expanded)
    if (not isinstance(document['disk'], dict) or set(document['disk']) != set(budget)
            or any(type(document['disk'][key]) is not int or document['disk'][key] != value for key, value in budget.items())):
        _error('Transport disk-space budget mismatch')
    return expected_names


def verify_transport(manifest_path: Path, *, include_entries=False) -> dict:
    """Validate exact byte volumes and original receipt, without merging files.

    Header generators can request validated_zip_entries. File hashes come from
    the inventory inside the SHA-bound accepted ZIP, not from untrusted names
    in this transport document. Payload bodies are not re-decompressed or
    CRC-tested here, and inference is not repeated: their trust is the exact
    archive SHA bound to prior acceptance. This does not attest Windows restore.
    """
    manifest_path = _plain(manifest_path)
    directory = manifest_path.parent
    raw_document = _read(manifest_path, MAX_MANIFEST_BYTES)
    document = _json(raw_document)
    expected_names = _validate_document(document, manifest_path.name)
    if {path.name for path in directory.iterdir()} != expected_names:
        _error('Transport directory has missing or unexpected files')
    for name in expected_names:
        _plain(directory / name)
    snapshots = {name: _snapshot(directory / name) for name in expected_names}
    archive = document['archive']
    combined = hashlib.sha256()
    for part in document['parts']:
        digest, size = hashlib.sha256(), 0
        with _open(directory / part['name']) as stream:
            for block in iter(lambda: stream.read(CHUNK_BYTES), b''):
                digest.update(block)
                combined.update(block)
                size += len(block)
        if size != part['size'] or digest.hexdigest() != part['sha256']:
            _error(f'Volume checksum/size mismatch: {part["name"]}')
    if combined.hexdigest() != archive['sha256']:
        _error('Joined volume SHA-256 does not match the accepted original ZIP')
    sidecars = {}
    for key in ('receipt', 'checksum'):
        record = document[key]
        sidecars[key] = _read(directory / record['name'], MAX_MANIFEST_BYTES)
        if _record(directory / record['name'], sidecars[key]) != record:
            _error('Original acceptance/checksum bytes changed')
    if sidecars['checksum'].decode('utf-8').strip() != f'{archive["sha256"]}  {archive["name"]}':
        _error('Original checksum does not bind this archive name/SHA')
    # Existing guard remains authoritative for required acceptance/GPU checks.
    validate_acceptance_receipt(directory / document['receipt']['name'], Path(archive['name']),
                                archive['sha256'], archive['identity'])
    reader = _VolumeZip(directory, document['parts'], archive['size'])
    try:
        identity, expanded, rows = _inspect_zip(reader, archive['name'])
    finally:
        reader.close()
    if (identity != archive['identity'] or expanded != archive['uncompressed_bytes']
            or len(rows) != archive['entry_count']):
        _error('Transport identity/entry/size metadata disagrees with the original ZIP')
    if _read(manifest_path, MAX_MANIFEST_BYTES) != raw_document:
        _error('Transport manifest changed during verification')
    if {path.name for path in directory.iterdir()} != expected_names:
        _error('Transport directory changed during verification')
    if any(_snapshot(directory / name) != before for name, before in snapshots.items()):
        _error('Transport bytes/paths changed during verification')
    if include_entries:
        document['validated_zip_entries'] = rows
    return document


def _publish_directory(source, destination):
    """Atomically publish without ever replacing even an empty user directory."""
    _plain(destination.parent, directory=True)
    if os.name == 'nt':
        # Windows rename does not replace an existing destination directory.
        os.rename(source, destination)
    elif sys.platform.startswith('linux'):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, 'renameat2', None)
        if rename is None:
            _error('Host lacks atomic no-replace directory publication')
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            code = ctypes.get_errno()
            if code in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
                _error('Filesystem lacks atomic no-replace directory publication')
            raise OSError(code, os.strerror(code), str(destination))
    else:
        _error('Atomic create is supported on Windows/Linux only; this host can verify')


def _write(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def create_transport(archive_path: Path, output_directory: Path, *, part_bytes=DEFAULT_PART_BYTES) -> Path:
    """Publish a NEW transport directory only after complete offline verification."""
    # Fail before opening/hashing a potentially multi-gigabyte source or
    # writing any volume on hosts without our supported atomic publication.
    if os.name != 'nt' and not sys.platform.startswith('linux'):
        _error('Atomic create is supported on Windows/Linux only; this host can verify')
    _integer(part_bytes, 1, ASSET_LIMIT - 1, 'part size (must be under 2 GiB)')
    archive_path = _plain(archive_path)
    output_directory = Path(output_directory).absolute()
    if '..' in output_directory.parts:
        _error('Parent traversal is not allowed in the output directory')
    _plain(output_directory.parent, directory=True)
    if os.path.lexists(output_directory):
        _error('Transport output already exists; never overwrite user paths')
    checksum_path = archive_path.with_suffix('.zip.sha256')
    receipt_path = archive_path.with_suffix('.zip.acceptance.json')
    checksum = _read(checksum_path, MAX_MANIFEST_BYTES)
    receipt = _read(receipt_path, MAX_MANIFEST_BYTES)
    size, digest = _digest(archive_path)
    if checksum.decode('utf-8').strip() != f'{digest}  {archive_path.name}':
        _error('Source checksum does not match the original ZIP')
    with _open(archive_path) as stream:
        identity, expanded, entries = _inspect_zip(_SingleZip(stream, size), archive_path.name)
    validate_acceptance_receipt(receipt_path, archive_path, digest, identity)
    if not 1 <= (size + part_bytes - 1) // part_bytes <= MAX_PARTS:
        _error('Source would require an unsupported number of transport volumes')
    if shutil.disk_usage(output_directory.parent).free < size + len(checksum) + len(receipt) + MAX_MANIFEST_BYTES + RESERVE_BYTES:
        _error('Insufficient free disk space to create byte volumes safely')
    staging = Path(tempfile.mkdtemp(prefix='.' + output_directory.name + '.partial-', dir=output_directory.parent))
    try:
        parts, offset, combined = [], 0, hashlib.sha256()
        with _open(archive_path) as source:
            while offset < size:
                name = f'{archive_path.name}.part{len(parts) + 1:03d}'
                count, hasher = min(part_bytes, size - offset), hashlib.sha256()
                with (staging / name).open('xb') as target:
                    remaining = count
                    while remaining:
                        block = source.read(min(CHUNK_BYTES, remaining))
                        if not block:
                            _error('Original ZIP was truncated during volume creation')
                        target.write(block)
                        hasher.update(block)
                        combined.update(block)
                        remaining -= len(block)
                    target.flush()
                    os.fsync(target.fileno())
                parts.append({'name': name, 'index': len(parts) + 1, 'offset': offset,
                              'size': count, 'sha256': hasher.hexdigest()})
                offset += count
            if source.read(1) or combined.hexdigest() != digest:
                _error('Original ZIP changed during volume creation')
        if _read(receipt_path, MAX_MANIFEST_BYTES) != receipt or _read(checksum_path, MAX_MANIFEST_BYTES) != checksum:
            _error('Original acceptance/checksum changed during volume creation')
        _write(staging / receipt_path.name, receipt)
        _write(staging / checksum_path.name, checksum)
        document = {'schema': 1, 'format': FORMAT,
                    'archive': {'name': archive_path.name, 'size': size, 'sha256': digest,
                                'root': archive_path.stem, 'uncompressed_bytes': expanded,
                                'entry_count': len(entries), 'identity': identity},
                    'part_bytes': part_bytes, 'parts': parts,
                    'receipt': _record(receipt_path, receipt), 'checksum': _record(checksum_path, checksum),
                    'disk': _budget(size, expanded), 'gpu_inference_verified': False,
                    'client_restore_verified': False}
        manifest_name = archive_path.name + '.transport.json'
        _write(staging / manifest_name, (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        verify_transport(staging / manifest_name)
        _publish_directory(staging, output_directory)
        return output_directory / manifest_name
    finally:
        # Only this call's private staging directory; never delete the target
        # or stale interrupted directories belonging to another invocation.
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create', help='Create byte volumes from an already accepted original ZIP')
    create.add_argument('--archive', type=Path, required=True)
    create.add_argument('--output-dir', type=Path, required=True)
    create.add_argument('--part-bytes', type=int, default=DEFAULT_PART_BYTES)
    verify = commands.add_parser('verify', help='Stream-check exact volumes, original ZIP SHA and acceptance')
    verify.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    manifest = create_transport(args.archive, args.output_dir, part_bytes=args.part_bytes) if args.command == 'create' else args.manifest
    document = verify_transport(manifest)
    print(json.dumps({'result': 'passed', 'scope': 'build-side-byte-transport-only',
                      'manifest': str(manifest), 'archive_sha256': document['archive']['sha256'],
                      'part_count': len(document['parts']), 'client_restore_verified': False,
                      'gpu_inference_verified': False}, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise SystemExit(f'Release transport blocked: {error}')
