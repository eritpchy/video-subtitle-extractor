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


def assert_static_imports(output: str) -> list[str]:
    imports = sorted({value.upper() for value in re.findall(r'^\s*([A-Za-z0-9_.-]+\.dll)\s*$', output, re.MULTILINE | re.IGNORECASE)})
    allowed = {'KERNEL32.DLL', 'ADVAPI32.DLL', 'BCRYPT.DLL', 'USER32.DLL'}
    if not imports or not set(imports) <= allowed: raise RuntimeError(f'Unexpected dynamic imports; /MT closure not established: {imports}')
    return imports


def tool_record(path: Path) -> dict:
    return {'path': str(path.resolve(strict=True)), 'sha256': digest(path), 'bytes': path.stat().st_size}


def compile_fixed(source_directory: Path, header_directory: Path, output: Path) -> dict:
    """Compile a fixed verified header; no transport imports/network involved."""
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'): raise RuntimeError('Native Windows x64 Server build required')
    compiler, linker, dumpbin = (shutil.which(name) for name in ('cl.exe', 'link.exe', 'dumpbin.exe'))
    if not all((compiler, linker, dumpbin)): raise RuntimeError('Run in the x64 MSVC Native Tools build environment')
    if Path(compiler).resolve().parent != Path(linker).resolve().parent: raise RuntimeError('Compiler/linker tools are from different directories')
    process_environment = {**os.environ, 'VSLANG': '1033'}  # Child diagnostics only; never changes OS/user settings.
    executable = output / 'restore.exe'
    dependencies_path = output / 'source-dependencies.json'
    command = [compiler, '/nologo', '/Bv', '/std:c++17', '/EHsc', '/MT', '/O1', '/Os', '/W4', '/WX', '/utf-8', '/bigobj', '/GS', '/guard:cf', '/Brepro',
               '/sourceDependencies', str(dependencies_path), '/DUNICODE', '/D_UNICODE', '/D_WIN32_WINNT=0x0A00',
               f'/I{source_directory}', f'/I{header_directory}', str(source_directory / 'restorer.cpp'),
               f'/Fo{output / "restorer.obj"}', f'/Fe{executable}', '/link', '/VERBOSE:LIB', '/INCREMENTAL:NO', '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA', '/DEPENDENTLOADFLAG:0x800',
               '/MACHINE:X64', '/SUBSYSTEM:CONSOLE', '/MANIFEST:EMBED', "/MANIFESTUAC:level='asInvoker' uiAccess='false'"]
    built = subprocess.run(command, cwd=output, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', errors='replace', env=process_environment)
    (output / 'compiler.log').write_text(built.stdout, encoding='utf-8'); built.check_returncode()
    dependencies = subprocess.run([dumpbin, '/DEPENDENTS', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    imports = assert_static_imports(dependencies)
    (output / 'imports.txt').write_text(dependencies, encoding='utf-8')
    header_info = subprocess.run([dumpbin, '/HEADERS', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    (output / 'pe-headers.txt').write_text(header_info, encoding='utf-8')
    if '8664 machine (x64)' not in header_info.lower(): raise RuntimeError('Expected x64 executable')
    loadconfig = subprocess.run([dumpbin, '/LOADCONFIG', str(executable)], check=True, capture_output=True, text=True, env=process_environment).stdout
    (output / 'load-config.txt').write_text(loadconfig, encoding='utf-8')
    if not re.search(r'^\s*0*800\s+Dependent Load Flags', loadconfig, re.MULTILINE | re.IGNORECASE): raise RuntimeError('System32-only static dependency load flag not established')
    actual = json.loads(dependencies_path.read_text(encoding='utf-8-sig'))
    includes = actual['Data']['Includes']
    if not includes or any(not isinstance(name, str) for name in includes): raise RuntimeError('Actual compiler header dependencies unavailable')
    # /sourceDependencies lists the headers actually consumed, not guessed SDK
    # paths from environment labels. /VERBOSE:LIB supplies resolved library paths.
    include_records = [tool_record(Path(name)) for name in sorted(set(includes))]
    libraries = sorted(set(re.findall(r'^\s*Searching\s+(.+?\.lib):\s*$', built.stdout, re.MULTILINE | re.IGNORECASE)))
    if not libraries: raise RuntimeError('Actual linker library provenance unavailable; inspect compiler.log')
    library_records = [tool_record(Path(name)) for name in libraries]
    sdk_headers = [row for row in include_records if re.search(r'Windows Kits[/\\].*[/\\]Include[/\\]', row['path'], re.IGNORECASE)]
    if not sdk_headers: raise RuntimeError('Actual Windows SDK header provenance unavailable')
    toolchain = {'tools': {'cl': tool_record(Path(compiler)), 'link': tool_record(Path(linker)), 'dumpbin': tool_record(Path(dumpbin))},
                 'diagnostic_language': '1033', 'compiler_bv_and_link_log': built.stdout, 'actual_headers': include_records, 'actual_libraries': library_records,
                 'actual_sdk_headers': sdk_headers, 'environment_labels_only': {name: os.environ.get(name) for name in ('VCToolsVersion', 'WindowsSDKVersion', 'WindowsSdkDir')},
                 'python': {'version': sys.version, 'executable': tool_record(Path(sys.executable))}}
    return {'command': command, 'toolchain': toolchain, 'imports': imports,
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
