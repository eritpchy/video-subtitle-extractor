#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate a fixed native header from fully verified accepted ZIP volumes.

Build host only. No unchecked JSON-to-code rendering. Existing validator
import dependencies are recorded when --provenance-output is supplied. The
validator rehashes all parts and the original ZIP, verifies acceptance identity,
and inspects ZIP members before any C++ metadata is emitted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
UINT64_MAX = (1 << 64) - 1


def _integer(value, label: str, *, minimum=0) -> int:
    if type(value) is not int or not minimum <= value <= UINT64_MAX:
        raise ValueError(f'Invalid {label}')
    return value


def _digest(value) -> str:
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid SHA256')
    return value


def _component(value: str):
    if (not value or value in ('.', '..') or value[-1] in '. '
            or any(ord(ch) < 32 or ch in '\\/:*?"<>|' for ch in value)):
        raise ValueError(f'Unsafe Windows path component: {value!r}')
    base = value.split('.', 1)[0].rstrip(' ').upper()
    if (base in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
            or re.fullmatch(r'(?:COM|LPT)[0-9¹²³]', base)):
        raise ValueError(f'Reserved Windows path component: {value!r}')
    if len(value.encode('utf-16-le', errors='strict')) // 2 > 255:
        raise ValueError('Windows component exceeds 255 UTF-16 units')


def _wide(name: str, value: str) -> str:
    # Only generated integer tokens enter source. Quotes, Unicode and backslash
    # from a filename can never end a C++ literal or inject a directive.
    data = value.encode('utf-16-le', errors='strict')
    units = [int.from_bytes(data[i:i + 2], 'little') for i in range(0, len(data), 2)]
    return f'inline constexpr wchar_t {name}[] = {{' + ','.join(f'0x{x:04x}' for x in units) + ',0};'


def render_verified(document: dict) -> str:
    """Internal pure renderer; production main always calls verify_transport."""
    archive = document['archive']
    identity = archive['identity']
    if identity['platform'] != 'windows' or identity['arch'] != 'x64':
        raise ValueError('Native restorer supports only Windows x64')
    if document['format'] != 'vse-zip-byte-volumes' or document['schema'] != 1:
        raise ValueError('Unsupported verified transport')
    _component(archive['name']); _component(archive['root'])
    if archive['name'] != archive['root'] + '.zip':
        raise ValueError('Archive/root mismatch')
    archive_size = _integer(archive['size'], 'ZIP size', minimum=1)
    archive_sha = _digest(archive['sha256'])
    unpacked = _integer(archive['uncompressed_bytes'], 'uncompressed size')
    if unpacked > 1024 ** 4:
        raise ValueError('Expanded archive exceeds the fixed one-TiB safety bound')
    index = document['fixed_zip_index']
    if (set(index) != {'schema', 'source_archive_sha256', 'central_directory_offset', 'files'}
            or index['schema'] != 1 or index['source_archive_sha256'] != archive_sha):
        raise ValueError('Fixed index does not bind this accepted ZIP')
    central_offset = _integer(index['central_directory_offset'], 'central directory offset', minimum=1)
    if central_offset >= archive_size:
        raise ValueError('Fixed index exceeds archive bounds')
    ranges = {}
    for row in index['files']:
        if set(row) != {'name', 'data_offset', 'compressed_bytes', 'method'}:
            raise ValueError('Unexpected fixed-index fields')
        if row['name'] in ranges:
            raise ValueError('Duplicate fixed-index file')
        offset = _integer(row['data_offset'], 'data offset', minimum=30)
        compressed = _integer(row['compressed_bytes'], 'compressed size')
        method = _integer(row['method'], 'compression method')
        if method not in (0, 8) or offset > central_offset or compressed > central_offset - offset:
            raise ValueError('Unsupported or out-of-bounds fixed range')
        ranges[row['name']] = (offset, compressed, method)
    last_end = 0
    for offset, compressed, _ in sorted(ranges.values()):
        if offset < last_end:
            raise ValueError('Fixed compressed ranges overlap')
        last_end = offset + compressed
    entries = {}
    for entry in document['validated_zip_entries']:
        name = entry['name'].rstrip('/')
        components = name.split('/')
        if len(components) > 256:
            raise ValueError('Native directory depth exceeds 256')
        for component in components: _component(component)
        if components[0] != archive['root']:
            raise ValueError('Member escapes accepted root')
        kind = entry['kind']
        if kind not in ('file', 'directory'):
            raise ValueError('Unsupported member type')
        size = _integer(entry['size'], 'entry size')
        digest = _digest(entry['sha256']) if kind == 'file' else ''
        if kind == 'directory' and size != 0:
            raise ValueError('Nonempty directory entry')
        key = '\\'.join(components)
        if kind == 'file':
            if name not in ranges:
                raise ValueError('Missing fixed-index file')
            _, compressed, method = ranges[name]
            if method == 0 and compressed != size:
                raise ValueError('STORE size mismatch')
        row = (size, kind == 'directory', digest)
        if key in entries and entries[key] != row:
            raise ValueError('Conflicting member')
        entries[key] = row
        for index in range(1, len(components)):
            parent = '\\'.join(components[:index])
            if parent in entries and not entries[parent][1]:
                raise ValueError('File used as directory')
            entries[parent] = (0, True, '')
    if len(entries) > 200_000 or any(len(name.encode('utf-16-le')) // 2 >= 32000 for name in entries):
        raise ValueError('Native inventory/path bound exceeded')
    if not entries or archive['root'] not in entries or not entries[archive['root']][1]:
        raise ValueError('Missing archive root')
    if len({unicodedata.normalize('NFC', name).casefold() for name in entries}) != len(entries):
        raise ValueError('Windows filename collision')
    if sum(row[0] for row in entries.values()) != unpacked:
        raise ValueError('Uncompressed inventory mismatch')
    if set(ranges) != {entry['name'] for entry in document['validated_zip_entries'] if entry['kind'] == 'file'}:
        raise ValueError('Fixed index has extra or missing files')
    parts = document['parts']
    if not isinstance(parts, list) or not parts:
        raise ValueError('Missing parts')
    total = 0
    lines = ['// SPDX-License-Identifier: Apache-2.0', '// Generated from fully verified accepted ZIP bytes. Do not edit.', '#pragma once', '#include "core.hpp"', 'namespace bundle {',
             _wide('archive_name', archive['name']), _wide('root_name', archive['root']),
             f'inline constexpr std::uint64_t archive_bytes = {archive_size}ULL;',
             f'inline constexpr char archive_sha256[] = "{archive_sha}";',
             f'inline constexpr std::uint64_t uncompressed_bytes = {unpacked}ULL;',
             f'inline constexpr std::uint64_t central_directory_offset = {central_offset}ULL;',
             f'inline constexpr std::uint64_t entry_count = {len(entries)}ULL;',
             f'inline constexpr std::size_t max_entry_path = {max(len(name.encode("utf-16-le")) // 2 for name in entries)};',
             _wide('accepted_identity', json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(',', ':')))]
    for index, part in enumerate(parts, 1):
        _component(part['name'])
        if part['name'] != f"{archive['name']}.part{index:03d}" or part['index'] != index or part['offset'] != total:
            raise ValueError('Part order/name/offset mismatch')
        size = _integer(part['size'], 'part size', minimum=1)
        _digest(part['sha256'])
        total += size
        lines.append(_wide(f'part_name_{index}', part['name']))
    if total != archive_size:
        raise ValueError('Part sizes do not equal accepted ZIP size')
    lines.append('inline constexpr vse::Part parts[] = {')
    for index, part in enumerate(parts, 1):
        lines.append(f'{{part_name_{index}, {part["size"]}ULL, "{part["sha256"]}"}},')
    lines.append('};')
    ordered = sorted(entries.items())
    for index, (name, _) in enumerate(ordered):
        lines.append(_wide(f'entry_name_{index}', name))
    lines.append('inline constexpr vse::Entry entries[] = {')
    for index, (name, (size, directory, digest)) in enumerate(ordered):
        offset, compressed, method = (0, 0, 0) if directory else ranges[name.replace('\\', '/')]
        lines.append(f'{{entry_name_{index}, {size}ULL, {str(directory).lower()}, "{digest}", {offset}ULL, {compressed}ULL, {method}U}},')
    lines += ['};', '} // namespace bundle', '']
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--provenance-output', type=Path)
    args = parser.parse_args()
    from scripts.release_volume_transport import verify_transport
    document = verify_transport(args.manifest, include_entries=True)
    from fixed_index import from_verified_transport
    document['fixed_zip_index'] = from_verified_transport(args.manifest, document)
    source = render_verified(document)
    # Never overwrite generated metadata silently; use a fresh build directory.
    with args.output.open('x', encoding='ascii', newline='\n') as handle:
        handle.write(source)
    if args.provenance_output:
        loaded = {}
        local = {}
        for name, module in sorted(sys.modules.items()):
            third_party = name == 'packaging' or name.startswith('packaging.')
            module_path = getattr(module, '__file__', None)
            if not third_party and module_path and Path(module_path).is_file():
                module_path = Path(module_path).resolve()
                if module_path.is_relative_to(ROOT.resolve()):
                    local[name] = {'relative_path': module_path.relative_to(ROOT.resolve()).as_posix(),
                                   'sha256': hashlib.sha256(module_path.read_bytes()).hexdigest()}
            if third_party:
                path = getattr(module, '__file__', None)
                if path and Path(path).is_file():
                    loaded[name] = {'path': path, 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        provenance = {'python': sys.version, 'third_party_modules_actually_loaded': loaded,
                      'local_modules_actually_loaded': local,
                      'packaging_version': getattr(sys.modules.get('packaging'), '__version__', None),
                      'fixed_header_rebuild_requires_these_modules': False}
        with args.provenance_output.open('x', encoding='utf-8') as handle:
            json.dump(provenance, handle, indent=2)
            handle.write('\n')
    print(f'Generated {args.output.name}: accepted ZIP SHA256 {document["archive"]["sha256"]}; header SHA256 {hashlib.sha256(source.encode("ascii")).hexdigest()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
