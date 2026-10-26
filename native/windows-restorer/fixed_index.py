# SPDX-License-Identifier: Apache-2.0
"""Build-side fixed compressed ranges, bound to accepted original ZIP SHA.

Not a customer ZIP parser. Layout parsing is only performed by the build host;
the restorer receives integer ranges in its immutable compiled metadata.
"""
from __future__ import annotations
from bisect import bisect_right
from contextlib import ExitStack
from pathlib import Path
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inspect_zip_layout as layout


def inspect_stream(stream, document):
    archive = document['archive']
    if layout.digest(stream) != archive['sha256']:
        raise ValueError('Fixed-index input differs from accepted ZIP SHA256')
    end = layout.end_records(stream, archive['size'])
    rows = layout.central_entries(stream, archive['size'], end)
    ordered = sorted(rows, key=lambda row: row['local_header_offset'])
    if not ordered or ordered[0]['local_header_offset'] != 0:
        raise ValueError('Empty/prepended ZIP cannot produce a fixed index')
    for index, row in enumerate(ordered):
        next_offset = ordered[index + 1]['local_header_offset'] if index + 1 < len(ordered) else end['central_directory_offset']
        layout.local_entry(stream, archive['size'], row, next_offset)
        if row['unsupported_reasons']:
            raise ValueError(f'Unsupported fixed-index ZIP member: {row["unsupported_reasons"]}')
    if layout.digest(stream) != archive['sha256']:
        raise ValueError('Fixed-index input changed while reading')
    expected = {row['name']: row for row in document['validated_zip_entries']}
    if len(expected) != len(document['validated_zip_entries']) or set(expected) != {row['name'] for row in rows}:
        raise ValueError('Fixed-index names differ from verified ZIP inventory')
    files = []
    for row in rows:
        entry = expected[row['name']]
        if entry['size'] != row['uncompressed_bytes'] or entry['kind'] != ('directory' if row['name'].endswith('/') else 'file'):
            raise ValueError('Fixed-index type/size differs from verified ZIP inventory')
        if entry['kind'] == 'file':
            files.append({'name': row['name'], 'data_offset': row['data_offset'],
                          'compressed_bytes': row['compressed_bytes'], 'method': row['method']})
    return {'schema': 1, 'source_archive_sha256': archive['sha256'],
            'central_directory_offset': end['central_directory_offset'], 'files': files}


class JoinedVolumes:
    """Seekable bounded view retaining every already-verified volume handle."""
    def __init__(self, directory, document, stack):
        from scripts.release_volume_transport import _open
        self.parts = document['parts']
        self.offsets = [row['offset'] for row in self.parts]
        self.size, self.position = document['archive']['size'], 0
        self.paths = [directory / row['name'] for row in self.parts]
        self.path_before = [path.stat() for path in self.paths]
        self.streams = [stack.enter_context(_open(path)) for path in self.paths]
        self.fd_before = [os.fstat(stream.fileno()) for stream in self.streams]
        if any(layout.identity(a) != layout.identity(b) or b.st_size != row['size']
               for a, b, row in zip(self.path_before, self.fd_before, self.parts)):
            raise ValueError('Volume changed while opening fixed-index input')

    def seek(self, offset, whence=0):
        value = offset + (0 if whence == 0 else self.position if whence == 1 else self.size if whence == 2 else -2 * self.size)
        if not 0 <= value <= self.size: raise ValueError('Fixed-index seek out of bounds')
        self.position = value
        return value

    def read(self, count):
        if not 0 <= count <= 1024 * 1024: raise ValueError('Fixed-index metadata read is not bounded')
        count = min(count, self.size - self.position)
        output = bytearray()
        while count:
            index = bisect_right(self.offsets, self.position) - 1
            local = self.position - self.offsets[index]
            length = min(count, self.parts[index]['size'] - local)
            if length <= 0: raise ValueError('Invalid fixed-index volume boundary')
            stream = self.streams[index]; stream.seek(local)
            chunk = stream.read(length)
            if len(chunk) != length: raise ValueError('Truncated fixed-index volume')
            output.extend(chunk); count -= length; self.position += length
        return bytes(output)

    def assert_unchanged(self):
        for path, stream, path_before, fd_before, row in zip(self.paths, self.streams, self.path_before, self.fd_before, self.parts):
            path_after, fd_after = path.stat(), os.fstat(stream.fileno())
            if (path.is_symlink() or fd_after.st_size != row['size'] or layout.snapshot(path_before) != layout.snapshot(path_after)
                    or layout.snapshot(fd_before) != layout.snapshot(fd_after)
                    or layout.identity(path_after) != layout.identity(fd_after)):
                raise ValueError('Fixed-index volume changed during inspection')


def from_verified_transport(manifest_path, document):
    with ExitStack() as stack:
        stream = JoinedVolumes(Path(manifest_path).absolute().parent, document, stack)
        result = inspect_stream(stream, document)
        stream.assert_unchanged()
        return result
