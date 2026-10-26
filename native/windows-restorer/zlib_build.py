# SPDX-License-Identifier: Apache-2.0
"""Offline compilation/provenance of the reviewed, immutable inflate closure."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess

PIN_SHA256 = '51584d81e0d94f4245169eef5f1c54004af81ad26bfe1638423a5a27c0ee225c'
SOURCES = ('inflate.c', 'inftrees.c', 'inffast.c', 'adler32.c', 'zutil.c')
HEADERS = ('zlib.h', 'zconf.h', 'zutil.h', 'inflate.h', 'inftrees.h', 'inffast.h', 'inffixed.h')
FILES = SOURCES + HEADERS + ('LICENSE', 'README')
RELATIVE_DIRECTORY = 'third_party/zlib-1.3.2'
MACROS = ('Z_SOLO', 'NO_GZIP')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources(source_directory):
    directory = Path(source_directory) / RELATIVE_DIRECTORY
    pin_path = directory / 'pin.json'
    if directory.is_symlink() or pin_path.is_symlink() or digest(pin_path) != PIN_SHA256:
        raise RuntimeError('Unreviewed zlib source pin')
    pin = json.loads(pin_path.read_bytes())
    if set(path.name for path in directory.iterdir()) != set(FILES) | {'pin.json'}:
        raise RuntimeError('Unexpected files in restricted zlib closure')
    if (set(pin['files']) != set(FILES) or pin['compiled_sources'] != list(SOURCES)
            or pin['build_macros'] != list(MACROS)):
        raise RuntimeError('Restricted zlib closure differs from reviewed policy')
    for name, row in pin['files'].items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or digest(path) != row['sha256']:
            raise RuntimeError(f'Changed upstream zlib source: {name}')
    return pin


def defined_symbols(output):
    symbols = set()
    for line in output.splitlines():
        if 'UNDEF' not in line and 'External' in line and '|' in line:
            symbols.add(line.rsplit('|', 1)[1].strip().lstrip('_'))
    return symbols


def assert_no_excluded_symbols(symbols):
    # Must be applied to all compiled objects AND the final link map. Merely
    # choosing static linking would not exclude vulnerable gzwrite code.
    bad = sorted(name for name in symbols if re.match(r'(?i)(gz|deflate|crc32|unz|zipOpen|zipClose)', name))
    if bad: raise RuntimeError(f'Excluded compression/archive symbols were linked: {bad}')


def compile_closure(source_directory, output, compiler, dumpbin, environment):
    source_directory, output = Path(source_directory), Path(output)
    pin = verify_sources(source_directory)
    directory = source_directory / RELATIVE_DIRECTORY
    records, objects, includes, symbols = [], [], set(), set()
    for name in SOURCES:
        stem = Path(name).stem
        obj = output / f'zlib-{stem}.obj'
        dependencies = output / f'zlib-{stem}-source-dependencies.json'
        command = [compiler, '/nologo', '/Bv', '/TC', '/std:c11', '/c', '/MT', '/O1', '/Os', '/W4', '/WX',
                   '/utf-8', '/GS', '/guard:cf', '/Brepro', '/DZ_SOLO', '/DNO_GZIP',
                   '/sourceDependencies', str(dependencies), f'/I{directory}', str(directory / name), f'/Fo{obj}']
        built = subprocess.run(command, cwd=output, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding='utf-8', errors='replace')
        (output / f'zlib-{stem}-compiler.log').write_text(built.stdout, encoding='utf-8')
        built.check_returncode()
        actual = json.loads(dependencies.read_text(encoding='utf-8-sig'))['Data']['Includes']
        if not actual or any(not isinstance(path, str) for path in actual): raise RuntimeError('Missing zlib include provenance')
        includes.update(actual)
        inspected = subprocess.run([dumpbin, '/SYMBOLS', str(obj)], env=environment, check=True, capture_output=True,
                                   encoding='utf-8', errors='replace').stdout
        (output / f'zlib-{stem}-symbols.txt').write_text(inspected, encoding='utf-8')
        defined = defined_symbols(inspected)
        if not defined: raise RuntimeError('No defined zlib object symbols found')
        assert_no_excluded_symbols(defined); symbols.update(defined)
        records.append({'source': name, 'command': command, 'object_sha256': digest(obj),
                        'compiler_bv_and_log': built.stdout, 'defined_symbols': sorted(defined)})
        objects.append(obj)
    if not {'inflateInit2_', 'inflate', 'inflateEnd'} <= symbols:
        raise RuntimeError('Required raw-inflate entry points missing')
    verify_sources(source_directory)
    return objects, {'pin': pin, 'pin_sha256': PIN_SHA256, 'compiled_objects': records,
                     'actual_header_paths': sorted(includes), 'defined_symbols': sorted(symbols),
                     'excluded_gzwrite_deflate_crc_archive_symbols_absent': True,
                     'scope': 'restricted-source-closure-not-a-claim-of-no-unknown-vulnerabilities'}


def inspect_link_map(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    symbols = set(re.findall(r'^\s*[0-9A-Fa-f]+:[0-9A-Fa-f]+\s+(\S+)\s+[0-9A-Fa-f]+\s', text, re.MULTILINE))
    if not {'inflateInit2_', 'inflate', 'inflateEnd'} <= symbols:
        raise RuntimeError('Final link map does not prove required raw-inflate entry points')
    assert_no_excluded_symbols(symbols)
    return {'sha256': digest(Path(path)), 'bytes': Path(path).stat().st_size,
            'excluded_gzwrite_deflate_crc_archive_symbols_absent': True,
            'required_inflate_symbols_present': True}
