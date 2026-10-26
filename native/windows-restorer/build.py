#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline MSVC /MT build with an exact-source/license companion; no publish."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def digest(path: Path) -> str:
    with path.open('rb') as handle: return hashlib.file_digest(handle, 'sha256').hexdigest()


def copy_notices(source: Path, output: Path):
    manifest = json.loads((ROOT / 'native/ocr/vc-runtime-manifest.json').read_text(encoding='utf-8'))
    rows = [row for row in manifest['files'] if row['name'] in ('license.docx', 'license-2015-2022.docx')]
    if len(rows) != 2: raise RuntimeError('Missing reviewed Microsoft runtime notice pins')
    output.mkdir()
    for row in rows:
        path = source / row['name']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or digest(path) != row['sha256']:
            raise RuntimeError(f'Missing/changed official runtime notice: {row["name"]}')
        shutil.copyfile(path, output / row['name'])
    shutil.copyfile(ROOT / 'LICENSE', output / 'Apache-2.0.txt')
    shutil.copyfile(HERE / 'THIRD_PARTY_NOTICES.txt', output / 'THIRD_PARTY_NOTICES.txt')
    shutil.copyfile(HERE / 'third_party/zlib-1.3.2/LICENSE', output / 'zlib-LICENSE.txt')


def assert_static_imports(output: str) -> list[str]:
    imports = sorted({value.upper() for value in re.findall(r'^\s*([A-Za-z0-9_.-]+\.dll)\s*$', output, re.MULTILINE | re.IGNORECASE)})
    allowed = {'KERNEL32.DLL', 'ADVAPI32.DLL', 'BCRYPT.DLL', 'USER32.DLL'}
    if not imports or not set(imports) <= allowed: raise RuntimeError(f'Unexpected dynamic imports; /MT closure not established: {imports}')
    return imports


def tool_record(path: Path) -> dict:
    return {'path': str(path.resolve(strict=True)), 'sha256': digest(path), 'bytes': path.stat().st_size}


def assert_pe_dependency_policy(executable: Path) -> dict:
    """Read the security field from PE bytes, never localized dumpbin prose.

    Self-contained stdlib code keeps the fixed-header source rebuild closed.
    PE32+ data-directory index 10 is the Load Configuration directory. In
    IMAGE_LOAD_CONFIG_DIRECTORY64, DependentLoadFlags is the WORD at 0x4e.
    https://learn.microsoft.com/en-us/windows/win32/debug/pe-format
    https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-image_load_config_directory64
    https://learn.microsoft.com/en-us/cpp/build/reference/dependentloadflag
    """
    if executable.is_symlink() or not executable.is_file():
        raise RuntimeError('Missing or linked PE executable')

    def identity(info):
        # Windows CPython 3.12 stat(path).ctime is creation time, whereas
        # fstat(fd).ctime is ChangeTime. Compare ctime only within one API;
        # never discard either API's before/after mutation check.
        # https://github.com/python/cpython/issues/157671
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                getattr(info, 'st_birthtime_ns', None))

    def snapshot(info):
        return (*identity(info), info.st_ctime_ns)

    before = executable.stat()
    with executable.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if identity(before) != identity(opened):
            raise RuntimeError('PE executable changed while opening')
        size = opened.st_size

        def read(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset > size or length > size - offset:
                raise RuntimeError('Truncated or out-of-bounds PE structure')
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise RuntimeError('Truncated PE structure')
            return data

        dos = read(0, 64)
        pe = struct.unpack_from('<I', dos, 0x3c)[0]
        if dos[:2] != b'MZ' or pe < 64 or read(pe, 4) != b'PE\0\0':
            raise RuntimeError('Invalid DOS/PE executable signature')
        coff = read(pe + 4, 20)
        machine, section_count = struct.unpack_from('<HH', coff)
        optional_size, characteristics = struct.unpack_from('<HH', coff, 16)
        if machine != 0x8664 or not characteristics & 0x0002 or characteristics & (0x0100 | 0x2000):
            raise RuntimeError('Expected an AMD64 x64 PE executable, not a DLL or 32-bit image')
        if not 1 <= section_count <= 96 or optional_size < 112 + 11 * 8:
            raise RuntimeError('Missing or invalid PE optional/section headers')
        optional_offset = pe + 24
        optional = read(optional_offset, optional_size)
        if struct.unpack_from('<H', optional)[0] != 0x20b:
            raise RuntimeError('Expected PE32+ optional header')
        directory_count = struct.unpack_from('<I', optional, 108)[0]
        if not 11 <= directory_count <= (optional_size - 112) // 8:
            raise RuntimeError('Missing or out-of-bounds PE LoadConfig data directory')
        image_size, headers_size = struct.unpack_from('<II', optional, 56)
        section_table = optional_offset + optional_size
        if not section_table + section_count * 40 <= headers_size <= size or image_size < headers_size:
            raise RuntimeError('Invalid PE image/header size')
        rva, directory_size = struct.unpack_from('<II', optional, 112 + 10 * 8)
        if not rva or directory_size < 80 or rva + directory_size > min(image_size, 0x100000000):
            raise RuntimeError('Missing, undersized or out-of-image PE LoadConfig directory')
        sections, virtual_ranges, raw_ranges = [], [], []
        for index in range(section_count):
            header = read(section_table + index * 40, 40)
            virtual_size, address, raw_size, raw = struct.unpack_from('<IIII', header, 8)
            span = max(virtual_size, raw_size)
            if span:
                if address < headers_size or address + span > min(image_size, 0x100000000):
                    raise RuntimeError('Invalid PE section virtual range')
                virtual_ranges.append((address, address + span))
            if raw_size:
                if raw < headers_size or raw > size or raw_size > size - raw:
                    raise RuntimeError('Invalid PE section raw-file range')
                raw_ranges.append((raw, raw + raw_size))
            sections.append((address, span, raw, raw_size))
        for ranges in (virtual_ranges, raw_ranges):
            ranges.sort()
            if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
                raise RuntimeError('Ambiguous overlapping PE sections')
        matches = [(raw + rva - address, raw_size - (rva - address))
                   for address, span, raw, raw_size in sections
                   if address <= rva < address + span and rva + directory_size <= address + span]
        if len(matches) != 1 or matches[0][1] < directory_size:
            raise RuntimeError('LoadConfig RVA does not map uniquely to file-backed PE bytes')
        offset, _ = matches[0]
        configuration = read(offset, 80)
        structure_size = struct.unpack_from('<I', configuration)[0]
        if not 80 <= structure_size <= directory_size:
            raise RuntimeError('LoadConfig structure Size does not contain DependentLoadFlags')
        flags = struct.unpack_from('<H', configuration, 0x4e)[0]
        if flags != 0x0800:
            raise RuntimeError(f'System32-only static dependency load flag not established in PE: 0x{flags:04x}')
        finished = os.fstat(stream.fileno())
        after = executable.stat()
        if (executable.is_symlink() or snapshot(before) != snapshot(after)
                or snapshot(opened) != snapshot(finished)
                or identity(finished) != identity(after)):
            raise RuntimeError('PE executable changed during policy verification '
                               f'(path before/after: {snapshot(before)!r}/{snapshot(after)!r}; '
                               f'fd before/after: {snapshot(opened)!r}/{snapshot(finished)!r})')
    return {'source': 'PE binary LoadConfig, not dumpbin text', 'machine': machine,
            'optional_header_magic': 0x20b, 'data_directory_index': 10,
            'load_config_rva': rva, 'load_config_file_offset': offset,
            'load_config_directory_size': directory_size, 'load_config_structure_size': structure_size,
            'dependent_load_flags_offset': 0x4e, 'dependent_load_flags': flags}


def compile_fixed(source_directory: Path, header_directory: Path, output: Path) -> dict:
    """Compile a fixed verified header; no transport imports/network involved."""
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'): raise RuntimeError('Native Windows x64 Server build required')
    compiler, linker, dumpbin = (shutil.which(name) for name in ('cl.exe', 'link.exe', 'dumpbin.exe'))
    if not all((compiler, linker, dumpbin)): raise RuntimeError('Run in the x64 MSVC Native Tools build environment')
    if Path(compiler).resolve().parent != Path(linker).resolve().parent: raise RuntimeError('Compiler/linker tools are from different directories')
    process_environment = {**os.environ, 'VSLANG': '1033'}  # Child diagnostics only; never changes OS/user settings.
    for option_variable in ('CL', '_CL_', 'LINK', '_LINK_'):
        process_environment.pop(option_variable, None)  # Fixed command, not inherited compiler/linker switches.
    executable = output / 'restore.exe'
    sys.path.insert(0, str(source_directory))
    import zlib_build
    zlib_objects, zlib_record = zlib_build.compile_closure(source_directory, output, compiler, dumpbin, process_environment)
    link_map = output / 'link-map.txt'
    dependencies_path = output / 'source-dependencies.json'
    command = [compiler, '/nologo', '/Bv', '/std:c++17', '/EHsc', '/MT', '/O1', '/Os', '/W4', '/WX', '/utf-8', '/bigobj', '/GS', '/guard:cf', '/Brepro',
               '/sourceDependencies', str(dependencies_path), '/DUNICODE', '/D_UNICODE', '/D_WIN32_WINNT=0x0A00',
               '/DZ_SOLO', '/DNO_GZIP', f'/I{source_directory}', f'/I{header_directory}', str(source_directory / 'restorer.cpp'),
               *[str(path) for path in zlib_objects],
               f'/Fo{output / "restorer.obj"}', f'/Fe{executable}', '/link', f'/MAP:{link_map}', '/VERBOSE:LIB', '/INCREMENTAL:NO', '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA', '/DEPENDENTLOADFLAG:0x800',
               '/MACHINE:X64', '/SUBSYSTEM:CONSOLE', '/MANIFEST:EMBED', "/MANIFESTUAC:level='asInvoker' uiAccess='false'"]
    built = subprocess.run(command, cwd=output, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', env=process_environment)
    (output / 'compiler.log').write_text(built.stdout, encoding='utf-8'); built.check_returncode()
    zlib_record['final_link_map'] = zlib_build.inspect_link_map(link_map)
    zlib_build.verify_sources(source_directory)
    dependencies = subprocess.run([dumpbin, '/DEPENDENTS', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    imports = assert_static_imports(dependencies)
    (output / 'imports.txt').write_text(dependencies, encoding='utf-8')
    header_info = subprocess.run([dumpbin, '/HEADERS', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    (output / 'pe-headers.txt').write_text(header_info, encoding='utf-8')
    loadconfig = subprocess.run([dumpbin, '/LOADCONFIG', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    (output / 'load-config.txt').write_text(loadconfig, encoding='utf-8')
    pe_policy = assert_pe_dependency_policy(executable)
    actual = json.loads(dependencies_path.read_text(encoding='utf-8-sig'))
    includes = actual['Data']['Includes']
    if not includes or any(not isinstance(name, str) for name in includes): raise RuntimeError('Actual compiler header dependencies unavailable')
    # /sourceDependencies lists the headers actually consumed, not guessed SDK
    # paths from environment labels. /VERBOSE:LIB supplies resolved library paths.
    include_records = [tool_record(Path(name)) for name in sorted(set(includes) | set(zlib_record['actual_header_paths']))]
    libraries = sorted(set(re.findall(r'^\s*Searching\s+(.+?\.lib):\s*$', built.stdout, re.MULTILINE | re.IGNORECASE)))
    if not libraries: raise RuntimeError('Actual linker library provenance unavailable; inspect compiler.log')
    library_records = [tool_record(Path(name)) for name in libraries]
    sdk_headers = [row for row in include_records if re.search(r'Windows Kits[/\\].*[/\\]Include[/\\]', row['path'], re.IGNORECASE)]
    if not sdk_headers: raise RuntimeError('Actual Windows SDK header provenance unavailable')
    toolchain = {'tools': {'cl': tool_record(Path(compiler)), 'link': tool_record(Path(linker)), 'dumpbin': tool_record(Path(dumpbin))},
                 'diagnostic_language': '1033', 'compiler_bv_and_link_log': built.stdout, 'actual_headers': include_records, 'actual_libraries': library_records,
                 'actual_sdk_headers': sdk_headers, 'environment_labels_only': {name: os.environ.get(name) for name in ('VCToolsVersion', 'WindowsSDKVersion', 'WindowsSdkDir')},
                 'python': {'version': sys.version, 'executable': tool_record(Path(sys.executable))}}
    return {'command': command, 'toolchain': toolchain, 'imports': imports, 'pe_dependency_policy': pe_policy,
            'restricted_zlib': zlib_record,
            'exe_sha256': digest(executable), 'exe_bytes': executable.stat().st_size,
            'bit_identical_rebuild_verified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--notices-directory', required=True, type=Path)
    parser.add_argument('--acknowledge-redistribution-license', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'): raise RuntimeError('Build on native Windows x64 Server CI')
    if not args.acknowledge_redistribution_license: raise RuntimeError('Distributor must confirm applicable Microsoft redistribution rights (no license is accepted by this tool)')
    import source_bundle
    original_git = source_bundle.git_identity(ROOT)
    snapshot = source_bundle.snapshot_sources(ROOT)
    output = args.output.absolute(); output.mkdir(parents=False, exist_ok=False)
    copy_notices(args.notices_directory, output / 'notices')
    manifest_bytes = args.manifest.read_bytes()
    subprocess.run([sys.executable, str(HERE / 'generate_metadata.py'), '--manifest', str(args.manifest.absolute()), '--output', str(output / 'bundle_metadata.hpp'), '--provenance-output', str(output / 'generator-dependencies.json')], check=True)
    record = compile_fixed(HERE, output, output)
    source_bundle.assert_sources_unchanged(ROOT, snapshot)
    if args.manifest.read_bytes() != manifest_bytes: raise RuntimeError('Transport manifest changed during build')
    record.update({'schema': 2, 'result': 'built-not-client-certified', 'git': original_git,
                   'git_after_build': source_bundle.git_identity(ROOT), 'generator_dependencies': json.loads((output / 'generator-dependencies.json').read_text(encoding='utf-8')),
                   'archive_transport_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'embedded_header_sha256': digest(output / 'bundle_metadata.hpp'),
                   'client_restore_verified': False, 'gpu_inference_verified': False,
                   'sources': {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()},
                   'notices': {p.name: digest(p) for p in (output / 'notices').iterdir()}})
    # Distribution is deliberately separate from obj, compiler logs and the host
    # environment. Source + fixed header + notices accompany the executable.
    distribution = output / 'distribution'; distribution.mkdir()
    source_record = source_bundle.create_source_bundle(distribution / 'restorer-source.zip', sources=snapshot,
        generated_header=(output / 'bundle_metadata.hpp').read_bytes(), notices={p.name: p.read_bytes() for p in (output / 'notices').iterdir()},
        build_record=record, transport_manifest=manifest_bytes)
    record['source_companion'] = source_record
    for destination in (output / 'build-record.json', distribution / 'build-record.json'):
        destination.write_text(json.dumps(record, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    shutil.copyfile(output / 'restore.exe', distribution / 'restore.exe')
    shutil.copytree(output / 'notices', distribution / 'notices')
    print(f'Built review-only distribution at {distribution}; requires Server 2022/2025 restore gates before distribution.')
    return 0


if __name__ == '__main__': raise SystemExit(main())
