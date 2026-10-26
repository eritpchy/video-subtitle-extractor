# SPDX-License-Identifier: Apache-2.0
"""Read-only ZIP layout evidence; no extraction, payload output, or acceptance claim.

Python 3.10+ standard library only. The expected SHA must come from the separately
accepted original ZIP, not from this tool. Run on that original file, not on an
unverified download. Strictly supports conventional single-disk ZIP32/ZIP64 layout;
unknown compression/encryption is reported as unsupported, never silently accepted.
This is an inspection tool, NOT the production fixed-index generator or ZIP guard.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys

MAX_ENTRIES = 1_000_000
MAX_CENTRAL_BYTES = 512 * 1024 * 1024
MAX_ZIP64_END_BYTES = 16 * 1024


class LayoutError(ValueError):
    """Malformed, ambiguous, changing, or unsupported archive layout."""
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def require(condition, message):
    if not condition:
        raise LayoutError(message)


def read_at(stream, offset, count, limit):
    require(0 <= offset <= limit and 0 <= count <= limit - offset, 'Read exceeds archive boundary')
    stream.seek(offset)
    value = stream.read(count)
    require(len(value) == count, 'Truncated archive')
    return value


def digest(stream):
    stream.seek(0)
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.hexdigest()


def extra_fields(raw):
    fields = {}
    position = 0
    while position < len(raw):
        require(len(raw) - position >= 4, 'Truncated extra-field header')
        tag, count = struct.unpack_from('<HH', raw, position)
        position += 4
        require(count <= len(raw) - position, 'Truncated extra-field body')
        require(tag not in fields, f'Duplicate extra-field tag {tag:#x}')
        fields[tag] = raw[position:position + count]
        position += count
    return fields


def zip64_values(fields, uncompressed, compressed, offset=None, disk=None):
    values = [uncompressed, compressed, offset, disk]
    widths = [8, 8, 8, 4]
    sentinels = [0xffffffff] * 3 + [0xffff]
    requested = [index for index, value in enumerate(values) if value is not None and value == sentinels[index]]
    raw = fields.get(1, b'')
    position = 0
    for index in requested:
        width = widths[index]
        require(len(raw) - position >= width, 'Missing/truncated ZIP64 extra field')
        values[index] = int.from_bytes(raw[position:position + width], 'little')
        position += width
    # Trailing ZIP64 data is ambiguous. The fixed-index candidate does not need
    # compatibility heuristics or speculative field order for redundant values.
    require(len(raw) == position, 'Unexpected/redundant ZIP64 extra-field bytes')
    return values, bool(requested)


def end_records(stream, size):
    require(size >= 22, 'Archive is shorter than EOCD')
    start = max(0, size - 22 - 65535)
    tail = read_at(stream, start, size - start, size)
    candidates = []
    cursor = 0
    while True:
        index = tail.find(b'PK\x05\x06', cursor)
        if index < 0:
            break
        if len(tail) - index >= 22:
            values = struct.unpack_from('<4s4H2IH', tail, index)
            if index + 22 + values[-1] == len(tail):
                candidates.append((start + index, values))
        cursor = index + 1
    require(len(candidates) == 1, 'Missing or ambiguous EOCD; trailing bytes are not supported')
    position, values = candidates[0]
    _, disk, central_disk, on_disk, count, central_bytes, central_offset, comment_bytes = values
    require(disk == 0 and central_disk == 0 and on_disk == count, 'Multi-disk ZIP is not supported')
    end_start = position
    zip64 = None
    if position >= 20 and read_at(stream, position - 20, 4, size) == b'PK\x06\x07':
        _, locator_disk, end_offset, disks = struct.unpack('<4sIQI', read_at(stream, position - 20, 20, size))
        require(locator_disk == 0 and disks == 1, 'Multi-disk ZIP64 is not supported')
        raw = read_at(stream, end_offset, 56, size)
        signature, length, made, needed, disk64, central_disk64, on_disk64, count64, bytes64, offset64 = struct.unpack('<4sQ2H2I4Q', raw)
        require(signature == b'PK\x06\x06' and 44 <= length <= MAX_ZIP64_END_BYTES - 12, 'Invalid ZIP64 EOCD')
        require(end_offset + 12 + length == position - 20, 'ZIP64 end records overlap or have gaps')
        require(disk64 == central_disk64 == 0 and on_disk64 == count64, 'Multi-disk ZIP64 EOCD')
        for value, large, sentinel in ((count, count64, 0xffff), (central_bytes, bytes64, 0xffffffff), (central_offset, offset64, 0xffffffff)):
            require(value == sentinel or value == large, 'ZIP32/ZIP64 EOCD disagreement')
        count, central_bytes, central_offset = count64, bytes64, offset64
        end_start = end_offset
        zip64 = {'offset': end_offset, 'bytes': length + 12, 'version_made_by': made, 'version_needed': needed,
                 'locator_offset': position - 20, 'sha256': hashlib.sha256(read_at(stream, end_offset, length + 12, size)).hexdigest()}
    else:
        require(count != 0xffff and central_bytes != 0xffffffff and central_offset != 0xffffffff, 'ZIP64 locator required')
    require(count <= MAX_ENTRIES and central_bytes <= MAX_CENTRAL_BYTES, 'Central directory exceeds inspection bounds')
    require(central_offset + central_bytes == end_start, 'Central directory/end records have gaps or overlap')
    require(count * 46 <= central_bytes, 'Impossible central-directory entry count')
    return {'eocd_offset': position, 'comment_bytes': comment_bytes, 'zip64': zip64,
            'central_directory_offset': central_offset, 'central_directory_bytes': central_bytes, 'entry_count': count}


def central_entries(stream, size, end):
    position = end['central_directory_offset']
    limit = position + end['central_directory_bytes']
    entries = []
    names, offsets = set(), set()
    for index in range(end['entry_count']):
        raw = read_at(stream, position, 46, limit)
        values = struct.unpack('<4s6H3I5H2I', raw)
        signature, made, needed, flags, method, time, date, crc, compressed32, uncompressed32, name_bytes, extra_bytes, comment_bytes, disk, internal, external, offset32 = values
        require(signature == b'PK\x01\x02' and name_bytes > 0, 'Invalid central-directory header')
        rest = read_at(stream, position + 46, name_bytes + extra_bytes + comment_bytes, limit)
        raw_name = rest[:name_bytes]
        require(b'\0' not in raw_name, 'NUL in member name')
        try:
            name = raw_name.decode('utf-8' if flags & 0x800 else 'cp437')
        except UnicodeDecodeError as error:
            raise LayoutError('Invalid UTF-8 member name') from error
        require(name not in names, 'Duplicate member name')
        names.add(name)
        fields = extra_fields(rest[name_bytes:name_bytes + extra_bytes])
        (uncompressed, compressed, offset, disk), is_zip64 = zip64_values(fields, uncompressed32, compressed32, offset32, disk)
        require(disk == 0 and offset < end['central_directory_offset'], 'Invalid local-header offset/disk')
        require(offset not in offsets, 'Overlapping/duplicate local-header offset')
        offsets.add(offset)
        entries.append({'index': index, 'name': name, 'method': method, 'flags': flags,
                        'version_made_by': made, 'version_needed': needed, 'crc32': f'{crc:08x}',
                        'compressed_bytes': compressed, 'uncompressed_bytes': uncompressed,
                        'local_header_offset': offset, 'central_header_offset': position,
                        'central_header_bytes': 46 + len(rest), 'central_header_sha256': hashlib.sha256(raw + rest).hexdigest(),
                        'central_zip64': is_zip64, 'central_extra_tags': sorted(fields),
                        'central_zip64_fields': [label for label, value, sentinel in (
                            ('uncompressed_bytes', uncompressed32, 0xffffffff),
                            ('compressed_bytes', compressed32, 0xffffffff),
                            ('local_header_offset', offset32, 0xffffffff), ('disk', values[13], 0xffff)) if value == sentinel],
                        'external_attributes': external, 'internal_attributes': internal,
                        '_name': raw_name, '_time': time, '_date': date, '_crc': crc})
        position += 46 + len(rest)
    require(position == limit, 'Unparsed central-directory bytes')
    return entries


def local_entry(stream, size, row, next_offset):
    position = row['local_header_offset']
    raw = read_at(stream, position, 30, next_offset)
    signature, needed, flags, method, time, date, crc, compressed32, uncompressed32, name_bytes, extra_bytes = struct.unpack('<4s5H3I2H', raw)
    require(signature == b'PK\x03\x04', 'Invalid local-header signature')
    local_fields = dict(version_needed=needed, flags=flags, method=method, dos_time=time, dos_date=date)
    central_fields = dict(version_needed=row['version_needed'], flags=row['flags'], method=row['method'],
                          dos_time=row['_time'], dos_date=row['_date'])
    # CPython 3.12 _write_end_record() promotes only the central version to 45
    # when the local-header offset needs ZIP64. The already-written local
    # header legitimately remains 20. Do not generalize this to size ZIP64,
    # other methods/versions, descriptors, or unrelated field disagreements.
    # https://github.com/python/cpython/blob/v3.12.10/Lib/zipfile/__init__.py
    offset_only_promotion = (needed == 20 and row['version_needed'] == 45 and method == 8
                             and flags & 8 == 0 and extra_bytes == 0
                             and compressed32 != 0xffffffff and uncompressed32 != 0xffffffff
                             and row['central_zip64_fields'] == ['local_header_offset'])
    differences = [key for key in local_fields if local_fields[key] != central_fields[key]]
    if differences and not (offset_only_promotion and differences == ['version_needed']):
        # Diagnostics do not relax validation. Exact fixed header bytes permit
        # a separately reviewed format correction rather than guessing damage.
        raise LayoutError('Local/central header fields disagree', {
            'entry_index': row['index'], 'entry_name': row['name'],
            'local_header_offset': position, 'central_header_offset': row['central_header_offset'],
            'local_fixed_header_hex': raw.hex(),
            'central_fixed_header_hex': read_at(stream, row['central_header_offset'], 46, size).hex(),
            'local_fields': local_fields, 'central_fields': central_fields,
            'differing_fields': differences,
            'central_zip64': row['central_zip64'], 'central_extra_tags': row['central_extra_tags'],
            'central_compressed_bytes': row['compressed_bytes'], 'central_uncompressed_bytes': row['uncompressed_bytes'],
            'local_compressed_32bit': compressed32, 'local_uncompressed_32bit': uncompressed32,
            'local_extra_bytes': extra_bytes, 'local_name_bytes': name_bytes,
            'whole_archive_initial_sha256_check_reached_and_passed': True,
            'whole_archive_final_sha256_check_reached': False})
    row['local_version_needed'] = needed
    row['version_relation'] = 'zip64_central_offset_only_20_to_45' if differences else 'equal'
    rest = read_at(stream, position + 30, name_bytes + extra_bytes, next_offset)
    require(rest[:name_bytes] == row['_name'], 'Local/central names disagree')
    fields = extra_fields(rest[name_bytes:])
    (uncompressed, compressed, _, _), is_zip64 = zip64_values(fields, uncompressed32, compressed32)
    descriptor_present = bool(flags & 8)
    expected = (row['_crc'], row['compressed_bytes'], row['uncompressed_bytes'])
    if descriptor_present:
        require(crc in (0, expected[0]) and compressed in (0, expected[1]) and uncompressed in (0, expected[2]), 'Local descriptor placeholders disagree with central directory')
    else:
        require((crc, compressed, uncompressed) == expected, 'Local/central CRC or sizes disagree')
    data_offset = position + 30 + len(rest)
    data_end = data_offset + row['compressed_bytes']
    require(data_end <= next_offset, 'Compressed member exceeds next record')
    descriptor = None
    if descriptor_present:
        length = next_offset - data_end
        require(length in (12, 16, 20, 24), 'Unsupported/overlapping data descriptor')
        descriptor_raw = read_at(stream, data_end, length, next_offset)
        signature_present = length in (16, 24)
        require(not signature_present or descriptor_raw[:4] == b'PK\x07\x08', 'Invalid descriptor signature')
        body = descriptor_raw[4:] if signature_present else descriptor_raw
        wide = len(body) == 20
        actual = struct.unpack('<IQQ' if wide else '<III', body)
        require(actual == expected, 'Data descriptor disagrees with central directory')
        require(wide or not (is_zip64 or row['compressed_bytes'] > 0xffffffff or row['uncompressed_bytes'] > 0xffffffff), 'ZIP64 requires a 64-bit descriptor')
        descriptor = {'offset': data_end, 'bytes': length, 'signature_present': signature_present, 'zip64': wide,
                      'sha256': hashlib.sha256(descriptor_raw).hexdigest()}
    else:
        require(data_end == next_offset, 'Gap, overlap, or unexpected descriptor after member')
    row.update(local_header_bytes=30 + len(rest), local_header_sha256=hashlib.sha256(raw + rest).hexdigest(),
               local_zip64=is_zip64, local_extra_tags=sorted(fields), data_offset=data_offset, data_end=data_end,
               descriptor=descriptor, local_central_consistent=True)
    reasons = []
    if method not in (0, 8): reasons.append('compression_method_not_store_or_deflate')
    if flags & (1 | 0x40 | 0x2000): reasons.append('encryption')
    if flags & ~(2 | 4 | 8 | 0x800): reasons.append('unsupported_general_purpose_flags')
    if method == 0 and row['compressed_bytes'] != row['uncompressed_bytes']:
        reasons.append('stored_sizes_differ')
    row['unsupported_reasons'] = reasons
    for key in ('_name', '_time', '_date', '_crc'):
        del row[key]


def identity(value):
    # Windows CPython 3.12 can expose creation time through path.stat().ctime
    # and ChangeTime through fstat().ctime. Cross-API identity excludes ctime;
    # each API's complete before/after snapshot still detects metadata changes.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            getattr(value, 'st_birthtime_ns', None))


def snapshot(value):
    return (*identity(value), value.st_ctime_ns)


def inspect_archive(path, expected_sha256):
    path = Path(path)
    require(re.fullmatch('[0-9a-fA-F]{64}', expected_sha256) is not None, 'Expected SHA256 must have 64 hexadecimal digits')
    require(not path.is_symlink(), 'Archive must not be a symlink')
    path_before = path.stat()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), 'Archive must be a regular file')
        require(identity(path_before) == identity(before), 'Archive changed while opening')
        expected = expected_sha256.lower()
        require(digest(stream) == expected, 'Original archive SHA256 mismatch')
        end = end_records(stream, before.st_size)
        rows = central_entries(stream, before.st_size, end)
        ordered = sorted(rows, key=lambda row: row['local_header_offset'])
        require(not ordered or ordered[0]['local_header_offset'] == 0, 'Prepended/SFX data is not supported')
        for index, row in enumerate(ordered):
            next_offset = ordered[index + 1]['local_header_offset'] if index + 1 < len(ordered) else end['central_directory_offset']
            local_entry(stream, before.st_size, row, next_offset)
        require(ordered or end['central_directory_offset'] == 0, 'Unexpected data in empty archive')
        require(digest(stream) == expected, 'Original archive changed during inspection')
        finished, path_after = os.fstat(stream.fileno()), path.stat()
        require(not path.is_symlink() and snapshot(before) == snapshot(finished)
                and snapshot(path_before) == snapshot(path_after)
                and identity(finished) == identity(path_after), 'Archive metadata changed during inspection')
    return {'schema': 1, 'scope': 'read-only-original-zip-layout-not-acceptance-or-client-restore-proof',
            'archive': {'name': path.name, 'bytes': before.st_size, 'sha256': expected},
            'whole_sha256_verified_before_and_after': True, 'payload_decompressed': False,
            'end_records': end, 'entries': rows,
            'summary': {'entries': len(rows), 'method_counts': dict(sorted(collections.Counter(str(row['method']) for row in rows).items())),
                        'flag_counts': dict(sorted(collections.Counter(str(row['flags']) for row in rows).items())),
                        'local_zip64_entries': sum(row['local_zip64'] for row in rows),
                        'central_zip64_entries': sum(row['central_zip64'] for row in rows),
                        'descriptor_entries': sum(row['descriptor'] is not None for row in rows),
                        'utf8_flag_entries': sum(bool(row['flags'] & 0x800) for row in rows),
                        'uncompressed_bytes': sum(row['uncompressed_bytes'] for row in rows),
                        'compressed_bytes': sum(row['compressed_bytes'] for row in rows),
                        'unsupported_entries': sum(bool(row['unsupported_reasons']) for row in rows)},
            'fixed_index_compression_compatible': bool(rows) and all(not row['unsupported_reasons'] for row in rows),
            'path_safety_and_payload_inventory_revalidated': False,
            'client_restore_verified': False, 'gpu_inference_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True, help='SHA from independently accepted original ZIP')
    parser.add_argument('--output', type=Path, required=True, help='New JSON file; existing files are never overwritten')
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.is_symlink(), 'Output already exists')
    try:
        report = inspect_archive(args.archive, args.expected_sha256)
    except LayoutError as error:
        print(json.dumps({'schema': 1, 'scope': 'read-only-zip-layout-failure-not-acceptance',
                          'archive_name': args.archive.name, 'expected_sha256': args.expected_sha256,
                          'error': str(error), 'details': error.details, 'layout_accepted': False,
                          'client_restore_verified': False, 'gpu_inference_verified': False},
                         ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 2
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, indent=2, ensure_ascii=True)
        output.write('\n')
    print(json.dumps({'output': str(args.output), 'archive': report['archive'], 'summary': report['summary'],
                      'fixed_index_compression_compatible': report['fixed_index_compression_compatible']}, sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
