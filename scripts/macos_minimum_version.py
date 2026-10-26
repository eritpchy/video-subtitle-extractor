"""Read the actual maximum macOS deployment target of a complete staged tree.

Pure stdlib, read-only, no Mach-O edits or OS-compatibility claims. Every runtime
slice contributes, including non-host slices in universal files. Object/debug/
static archives are not runtime images. Structural bounds needed to read Mach-O
headers/load commands are checked; this is not a replacement for dyld, signing,
package integrity, or execution on the declared oldest macOS.

Format references (no vendor source is copied): Apple's mach-o/loader.h,
mach-o/fat.h and mach/machine.h in apple-oss-distributions/cctools and xnu.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
from typing import BinaryIO

THIN = {b'\xce\xfa\xed\xfe': ('<', 32), b'\xfe\xed\xfa\xce': ('>', 32),
        b'\xcf\xfa\xed\xfe': ('<', 64), b'\xfe\xed\xfa\xcf': ('>', 64)}
FAT = {b'\xca\xfe\xba\xbe': ('>', 32), b'\xbe\xba\xfe\xca': ('<', 32),
       b'\xca\xfe\xba\xbf': ('>', 64), b'\xbf\xba\xfe\xca': ('<', 64)}
RUNTIME_TYPES = {2: 'executable', 3: 'fixed-vm-library', 6: 'dylib', 7: 'dynamic-linker', 8: 'bundle'}
NON_RUNTIME_TYPES = {1: 'object', 4: 'core', 9: 'dylib-stub', 10: 'debug-symbols'}
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_MACOSX = 0x24
OTHER_VERSION_COMMANDS = {0x25, 0x2f, 0x30}
MAX_COMMAND_BYTES = 16 * 1024 * 1024
MAX_COMMANDS = 65536
MAX_SLICES = 64
AR_MAGIC = b'!<arch>\n'
THIN_AR_MAGIC = b'!<thin>\n'


def _fail(label: str, message: str):
    raise RuntimeError(f'{label}: {message}')


def version_text(encoded: int) -> str:
    result = f'{encoded >> 16}.{(encoded >> 8) & 255}'
    return result + (f'.{encoded & 255}' if encoded & 255 else '')


def _architecture(cpu: int, subtype: int, bits: int, label: str) -> str:
    base = subtype & 0xffffff
    choices = {
        7: (32, {3: 'i386', 4: 'i386', 132: 'i386', 5: 'i386', 22: 'i386', 54: 'i386', 86: 'i386', 118: 'i386', 10: 'i386', 26: 'i386'}),
        0x1000007: (64, {3: 'x86_64', 8: 'x86_64h'}),
        0x100000c: (64, {0: 'arm64', 1: 'arm64v8', 2: 'arm64e'}),
        18: (32, {value: 'ppc' for value in (*range(12), 100)}),
        0x1000012: (64, {0: 'ppc64', 100: 'ppc64'}),
    }
    if cpu not in choices or choices[cpu][0] != bits or base not in choices[cpu][1]:
        _fail(label, f'Unsupported CPU/subtype/header-width: {cpu:#x}/{subtype:#x}/{bits}')
    return choices[cpu][1][base]


class _Reader:
    def __init__(self, stream: BinaryIO, size: int, label: str):
        self.stream, self.size, self.label = stream, size, label

    def read(self, offset: int, count: int) -> bytes:
        if offset < 0 or count < 0 or offset + count > self.size:
            _fail(self.label, 'Read crosses the file boundary')
        self.stream.seek(offset)
        value = self.stream.read(count)
        if len(value) != count: _fail(self.label, 'File truncated while reading')
        return value


def _thin(reader: _Reader, offset: int, length: int, declared: tuple[int, int] | None = None) -> tuple[list[dict], bool]:
    magic = reader.read(offset, min(8, length))
    if magic in (AR_MAGIC, THIN_AR_MAGIC): return [], False
    if magic[:4] not in THIN: _fail(reader.label, 'Universal slice is not a supported thin Mach-O/static archive')
    endian, bits = THIN[magic[:4]]
    header_size = 32 if bits == 64 else 28
    if length < header_size: _fail(reader.label, 'Truncated Mach-O header')
    fields = struct.unpack(endian + ('8I' if bits == 64 else '7I'), reader.read(offset, header_size))
    _, cpu, subtype, kind, count, command_bytes, _ = fields[:7]
    if bits == 64 and fields[7]: _fail(reader.label, 'Nonzero reserved Mach-O header field')
    if declared is not None and declared != (cpu, subtype): _fail(reader.label, 'Universal table CPU/subtype differs from slice header')
    if kind not in RUNTIME_TYPES and kind not in NON_RUNTIME_TYPES: _fail(reader.label, f'Unsupported Mach-O file type {kind}')
    runtime = kind in RUNTIME_TYPES
    architecture = _architecture(cpu, subtype, bits, reader.label) if runtime else None
    if count > MAX_COMMANDS or command_bytes > MAX_COMMAND_BYTES or header_size + command_bytes > length or count * 8 > command_bytes:
        _fail(reader.label, 'Invalid/bounded Mach-O load-command region')
    commands = reader.read(offset + header_size, command_bytes)
    position, deployments, seen = 0, [], set()
    for _ in range(count):
        if position + 8 > len(commands): _fail(reader.label, 'Truncated load-command header')
        command, width = struct.unpack_from(endian + 'II', commands, position)
        if width < 8 or width % (8 if bits == 64 else 4) or position + width > len(commands):
            _fail(reader.label, 'Invalid load-command width/alignment/boundary')
        deployment_ids = {LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX, *OTHER_VERSION_COMMANDS}
        if command & 0x80000000 and command & 0x7fffffff in deployment_ids:
            _fail(reader.label, 'Invalid required-bit variant of deployment load command')
        if command in deployment_ids:
            if command in seen: _fail(reader.label, 'Duplicate deployment load command')
            seen.add(command)
            if command == LC_BUILD_VERSION:
                if width < 24: _fail(reader.label, 'Truncated LC_BUILD_VERSION')
                platform, minimum, sdk, tools = struct.unpack_from(endian + '4I', commands, position + 8)
                if width != 24 + tools * 8: _fail(reader.label, 'LC_BUILD_VERSION tool count/size mismatch')
                tool_rows = [struct.unpack_from(endian + 'II', commands, position + 24 + index * 8) for index in range(tools)]
                if len({row[0] for row in tool_rows}) != len(tool_rows): _fail(reader.label, 'Duplicate build-tool record')
                command_name = 'LC_BUILD_VERSION'
            else:
                if width != 16: _fail(reader.label, 'Invalid legacy deployment-command size')
                minimum, sdk = struct.unpack_from(endian + 'II', commands, position + 8)
                platform = 1 if command == LC_VERSION_MIN_MACOSX else None
                tool_rows = []
                command_name = 'LC_VERSION_MIN_MACOSX' if platform == 1 else f'unsupported-{command:#x}'
            if runtime:
                if platform != 1: _fail(reader.label, f'Non-macOS/unsupported runtime platform in {command_name}: {platform}')
                if minimum >> 16 < 10 or (sdk and sdk < minimum): _fail(reader.label, 'Invalid macOS deployment version/SDK bound')
                deployments.append({'command': command_name, 'minimum_encoded': minimum, 'sdk_encoded': sdk,
                                    'tools': [{'tool': tool, 'version_encoded': version} for tool, version in tool_rows]})
        position += width
    if position != command_bytes: _fail(reader.label, 'Load-command count does not exactly consume sizeofcmds')
    if not runtime: return [], False
    if not deployments: _fail(reader.label, 'Runtime Mach-O has no supported macOS deployment target')
    if len({row['minimum_encoded'] for row in deployments}) != 1:
        _fail(reader.label, 'Conflicting macOS deployment targets in one slice')
    sdk_values = {row['sdk_encoded'] for row in deployments if row['sdk_encoded']}
    if len(sdk_values) > 1: _fail(reader.label, 'Conflicting deployment SDKs in one slice')
    minimum = deployments[0]['minimum_encoded']
    return [{'cpu': cpu, 'cpu_subtype': subtype, 'architecture': architecture, 'bits': bits,
             'byte_order': 'little' if endian == '<' else 'big', 'platform': 'macos',
             'file_type': RUNTIME_TYPES[kind], 'slice_offset': offset, 'slice_size': length,
             'minimum_encoded': minimum, 'minimum_macos_version': version_text(minimum),
             'deployment_commands': deployments}], True


def _parse(reader: _Reader) -> list[dict]:
    magic = reader.read(0, min(8, reader.size))
    if magic in (AR_MAGIC, THIN_AR_MAGIC): return []
    if magic[:4] in THIN: return _thin(reader, 0, reader.size)[0]
    # CAFEBABE also prefixes Java class files. Ambiguous/malformed candidates
    # fail bounds/CPU checks rather than being reported as macOS runtime images.
    if magic[:4] not in FAT:
        if magic and len(magic) < 4 and any(key.startswith(magic) for key in (*THIN, *FAT)):
            _fail(reader.label, 'Truncated Mach-O magic')
        return []
    if reader.size < 8: _fail(reader.label, 'Truncated universal Mach-O header')
    endian, bits = FAT[magic[:4]]
    count = struct.unpack(endian + 'I', magic[4:8])[0]
    if not 1 <= count <= MAX_SLICES: _fail(reader.label, 'Invalid/bounded universal slice count')
    width = 32 if bits == 64 else 20
    table_end = 8 + count * width
    table = reader.read(8, count * width)
    slices, seen = [], set()
    for index in range(count):
        values = struct.unpack_from(endian + ('IIQQII' if bits == 64 else 'IIIII'), table, index * width)
        cpu, subtype, offset, length, alignment = values[:5]
        if bits == 64 and values[5]: _fail(reader.label, 'Nonzero reserved universal field')
        if ((cpu, subtype) in seen or alignment > (63 if bits == 64 else 31) or offset % (1 << alignment)
                or offset < table_end or length < 4 or offset + length > reader.size):
            _fail(reader.label, 'Duplicate/misaligned/out-of-bounds universal slice')
        seen.add((cpu, subtype)); slices.append((offset, length, cpu, subtype))
    ordered = sorted(slices)
    if any(a[0] + a[1] > b[0] for a, b in zip(ordered, ordered[1:])): _fail(reader.label, 'Overlapping universal slices')
    result, classifications = [], set()
    for offset, length, cpu, subtype in slices:
        rows, runtime = _thin(reader, offset, length, (cpu, subtype))
        result.extend(rows); classifications.add(runtime)
    if len(classifications) > 1: _fail(reader.label, 'Mixed runtime/non-runtime universal slices')
    return result


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def macho_minimums(source: Path | bytes, *, label: str | None = None) -> list[dict]:
    """Return runtime slice records, [] for non-runtime bytes; malformed fails.

    Path input reads only bounded headers/command tables, never all model/source
    data. Byte input is useful for deterministic structural tests. All supported
    runtime slices are returned; architecture filtering is never performed.
    """
    if isinstance(source, bytes): return _parse(_Reader(io.BytesIO(source), len(source), label or '<bytes>'))
    path = Path(source)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode): _fail(str(path), 'Expected a non-symlink regular file')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before): _fail(str(path), 'Input changed while opening')
        result = _parse(_Reader(stream, before.st_size, label or str(path)))
        if _stamp(os.fstat(stream.fileno())) != _stamp(before): _fail(str(path), 'Input changed during scan')
    if _stamp(path.lstat()) != _stamp(before): _fail(str(path), 'Input path changed during scan')
    return result


def _tree(root: Path) -> tuple[dict, dict, list[Path]]:
    snapshot, links, files, edges = {}, {}, [], {'': set()}
    pending = [root]
    while pending:
        directory = pending.pop()
        parent = directory.relative_to(root).as_posix() if directory != root else ''
        edges.setdefault(parent, set())
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(root).as_posix(); metadata = path.lstat()
            snapshot[relative] = _stamp(metadata)
            if stat.S_ISLNK(metadata.st_mode):
                try: target = path.resolve(strict=True)
                except (OSError, RuntimeError) as error: raise RuntimeError(f'Broken/cyclic staged symlink: {relative}') from error
                if not target.is_relative_to(root) or target == root: _fail(relative, 'Staged symlink escapes/loops to the root')
                if not (target.is_file() or target.is_dir()): _fail(relative, 'Staged symlink targets a special file')
                links[relative] = target.relative_to(root).as_posix()
                if target.is_dir(): edges[parent].add(links[relative])
            elif stat.S_ISDIR(metadata.st_mode):
                edges[parent].add(relative); pending.append(path)
            elif stat.S_ISREG(metadata.st_mode): files.append(path)
            else: _fail(relative, 'Special file cannot be silently omitted from staged scan')
    # The physical walk never follows links, but a directory-alias cycle must
    # still be diagnosed rather than left invisible in the resulting package.
    colors = {}
    for start in edges:
        if colors.get(start): continue
        stack = [(start, False)]
        while stack:
            node, finished = stack.pop()
            if finished:
                colors[node] = 2
                continue
            if colors.get(node) == 1: _fail(node or '.', 'Cyclic staged directory aliases')
            if colors.get(node) == 2: continue
            colors[node] = 1; stack.append((node, True))
            stack.extend((child, False) for child in sorted(edges.get(node, ()), reverse=True))
    return snapshot, links, sorted(files)


def audit_minimum_system(root: Path, *, expected_arches: tuple[str, ...] | None = None) -> dict:
    """Scan every staged file; return the max over every runtime slice.

    Optional expected_arches requires those exact architecture labels in EACH
    runtime file; it does not filter other slices out of the reported maximum.
    Internal framework symlinks are recorded; their real files are scanned once
    per actual path. External/broken links and concurrent tree changes fail.
    """
    allowed = {'arm64', 'arm64v8', 'arm64e', 'x86_64', 'x86_64h', 'i386', 'ppc', 'ppc64'}
    if expected_arches is not None and (not isinstance(expected_arches, tuple) or not expected_arches or any(arch not in allowed for arch in expected_arches) or len(set(expected_arches)) != len(expected_arches)):
        raise ValueError('expected_arches must be distinct supported architecture labels, or None')
    root = Path(root)
    if root.is_symlink() or not root.is_dir(): _fail(str(root), 'Expected a real staged directory')
    root = root.resolve(strict=True)
    root_stamp = _stamp(root.stat())
    before, links, paths = _tree(root)
    images, hashes = {}, {}
    for path in paths:
        relative = path.relative_to(root).as_posix()
        rows = macho_minimums(path, label=relative)
        if not rows: continue
        if expected_arches is not None and not set(expected_arches) <= {row['architecture'] for row in rows}:
            _fail(relative, 'Runtime file does not include every required architecture')
        images[relative] = rows
        with path.open('rb') as stream: hashes[relative] = hashlib.file_digest(stream, 'sha256').hexdigest()
    after, final_links, _ = _tree(root)
    if before != after or links != final_links or root_stamp != _stamp(root.stat()): _fail(str(root), 'Staged tree changed during deployment scan')
    if not images: _fail(str(root), 'No runtime Mach-O deployment targets found')
    highest = max(row['minimum_encoded'] for values in images.values() for row in values)
    return {'schema': 1, 'minimum_macos_version': version_text(highest), 'minimum_encoded': highest,
            'images': images, 'file_sha256': hashes, 'symlinks': links,
            'architectures': sorted({row['architecture'] for values in images.values() for row in values}),
            'scanned_regular_files': len(paths), 'runtime_macho_files': len(images),
            'runtime_macho_slices': sum(len(values) for values in images.values()),
            'limiting_files': [name for name, values in images.items() if any(row['minimum_encoded'] == highest for row in values)],
            'scope': 'Highest declared macOS minimum over every runtime-loadable Mach-O slice; not oldest-OS execution proof'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-architecture', action='append')
    args = parser.parse_args()
    result = audit_minimum_system(args.stage, expected_arches=tuple(args.require_architecture) if args.require_architecture else None)
    text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8') as stream: stream.write(text)
    else: print(text, end='')
    return 0


if __name__ == '__main__': raise SystemExit(main())
