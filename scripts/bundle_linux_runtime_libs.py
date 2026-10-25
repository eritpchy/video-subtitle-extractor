#!/usr/bin/env python3
"""Build-time ELF dependency closure for trusted Linux release inputs.

Requires binutils and Debian/Ubuntu package metadata on the BUILD host only.
Never executes inspected ELF files. glibc and the ELF loader remain OS supplied;
manifest.json records the actual symbol-version floor, not universal portability.
Launch with LD_LIBRARY_PATH=<bundle>/native-libs. X11 server / graphics drivers
remain OS facilities; XCB client dependencies are bundled through Qt plugins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

BASE = re.compile(r"^(?:ld-linux[^/]*\.so(?:\.\d+)*|lib(?:c|m|dl|pthread|rt|util|resolv|anl|BrokenLocale|nss_(?:files|dns|compat|hesiod))\.so(?:\.\d+)*)$")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE, env={**os.environ, 'LC_ALL': 'C'})


def is_elf(path: Path) -> bool:
    with path.open('rb') as stream:
        return stream.read(4) == b'\x7fELF'


def safe_name(name: str) -> str:
    if not name or Path(name).name != name or name in {'.', '..'} or '\\' in name:
        raise ValueError(f'Unsafe library/package name: {name!r}')
    return name


def elf_info(path: Path) -> dict:
    dynamic = run('readelf', '-dW', str(path))
    versions = run('readelf', '-VW', str(path))
    # Definitions advertise versions exported by a library, not its requirements.
    needs = versions.split('Version needs section', 1)[-1] if 'Version needs section' in versions else ''
    return {
        'needed': re.findall(r'\(NEEDED\).*?\[(.*?)\]', dynamic),
        'paths': [p for entry in re.findall(r'\((?:RUNPATH|RPATH)\).*?\[(.*?)\]', dynamic) for p in entry.split(':')],
        'glibc': sorted(set(re.findall(r'Name: GLIBC_(\d+(?:\.\d+)+)', needs))),
        'glibc_abi': sorted(set(re.findall(r'Name: (GLIBC_[A-Za-z_][A-Za-z0-9_]*)', needs))),
    }


def package_provenance(source: Path, destination: Path) -> dict:
    """Fail closed if redistribution attribution cannot be recovered."""
    candidates = [source, source.resolve()]
    if str(source).startswith('/usr/'):
        candidates.append(Path(str(source)[4:]))
    elif str(source).startswith(('/lib/', '/lib64/')):
        candidates.append(Path('/usr' + str(source)))
    package = None
    for candidate in candidates:
        try:
            output = run('dpkg-query', '-S', str(candidate))
        except subprocess.CalledProcessError:
            continue
        matches = [line.rsplit(': ', 1)[0] for line in output.splitlines() if ': ' in line and line.rsplit(': ', 1)[1] == str(candidate)]
        if matches:
            package = matches[0]
            break
    if not package:
        raise RuntimeError(f'No Debian package/license provenance for {source}')
    safe_name(package)
    copyright_file = Path('/usr/share/doc') / package.split(':')[0] / 'copyright'
    if not copyright_file.is_file():
        raise RuntimeError(f'Missing copyright attribution: {copyright_file}')
    license_dir = destination / 'licenses' / package
    license_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(copyright_file, license_dir / 'copyright')
    copyright_text = copyright_file.read_text(errors='replace')
    for name in set(re.findall(r'/usr/share/common-licenses/([A-Za-z0-9.+_-]+)', copyright_text)):
        common = Path('/usr/share/common-licenses') / name.rstrip('.')
        if common.is_file():
            shutil.copyfile(common, license_dir / common.name)
        else:
            raise RuntimeError(f'Attribution refers to missing common license {common}')
    metadata = run('dpkg-query', '-W', '-f=${binary:Package}\n${Version}\n${source:Package}\n${source:Version}\n${Homepage}\n', package).splitlines()
    return {'package': package, 'package_metadata': metadata, 'copyright_source': str(copyright_file),
            'license_files': [str(p.relative_to(destination)) for p in sorted(license_dir.iterdir())]}


def linux_os_release() -> str:
    """Read real build-host provenance; absence is a release-blocking error."""
    return Path('/etc/os-release').read_text()


def bundle_linux_libraries(stage_root: Path, runtime_python: Path | None = None) -> dict:
    root = Path(stage_root).resolve(strict=True)
    dest = root / 'native-libs'
    if dest.exists():
        raise ValueError(f'Refusing to overwrite existing closure: {dest}')
    # Do not traverse symlinked directories or permit input files outside bundle.
    initial = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                raise ValueError(f'Escaping bundle symlink: {path}')
        for name in files:
            path = Path(parent) / name
            if path.is_file() and is_elf(path):
                initial.append(path)
    if not initial:
        raise ValueError('No ELF inputs found in staged bundle')
    cache = {}
    for line in run('/sbin/ldconfig', '-p').splitlines():
        match = re.match(r'\s*(\S+)\s+.*?=>\s+(\S+)', line)
        if match:
            cache.setdefault(match[1], Path(match[2]))
    index = {}
    for path in initial:
        index.setdefault(path.name, []).append(path)
    dest.mkdir()
    records, scanned, base, versions = {}, {}, set(), set()
    origins = {}
    abi_requirements = set()
    queue = list(initial)
    try:
        while queue:
            binary = queue.pop()
            real = binary.resolve()
            if real in scanned:
                continue
            info = elf_info(binary)
            scanned[real] = info
            versions.update(info['glibc'])
            abi_requirements.update(info.get('glibc_abi', []))
            origin = origins.get(binary, binary)
            search = [binary.parent, origin.parent]
            for value in info['paths']:
                expanded = value.replace('${ORIGIN}', str(origin.parent)).replace('$ORIGIN', str(origin.parent))
                if '$' not in expanded and Path(expanded).is_absolute():
                    search.append(Path(expanded))
            for name in info['needed']:
                safe_name(name)
                if BASE.fullmatch(name):
                    base.add(name)
                    continue
                candidates = [p / name for p in search] + [dest / name]
                candidates += index.get(name, [])
                candidate = next((p for p in candidates if p.is_file() and p.resolve().is_relative_to(root)), None)
                if candidate is not None:
                    queue.append(candidate)
                    continue
                if name.startswith(('libQt5', 'libQt6', 'libpyside', 'libshiboken')):
                    raise RuntimeError(f'Missing vendor Qt library {name} required by {binary.relative_to(root)}; refusing ABI-unsafe host Qt substitution')
                candidate = next((p for p in candidates if p.is_file()), cache.get(name))
                if candidate is None or not candidate.is_file():
                    raise RuntimeError(f'Unresolved dependency {name} required by {binary.relative_to(root)}')
                source = candidate.resolve(strict=True)
                target = dest / name
                provenance = package_provenance(source, dest)
                shutil.copyfile(source, target)
                target.chmod(0o755)
                records[name] = {'source': str(source), 'sha256': sha256(target), 'bytes': target.stat().st_size, **provenance}
                origins[target] = source
                queue.append(target)
        floor = max(versions, key=lambda x: tuple(map(int, x.split('.')))) if versions else None
        manifest = {'schema_version': 1, 'base_libraries': sorted(base), 'minimum_glibc': floor, 'glibc_non_numeric_requirements': sorted(abi_requirements),
                    'build_os_release': linux_os_release(),
                    'launch_environment': {'LD_LIBRARY_PATH': '$BUNDLE/native-libs'},
                    'limitations': ['Requires Linux with recorded glibc ABI or newer.', 'Native GUI requires an OS X11 display server. GPU/GL drivers are OS facilities.', 'DT_NEEDED closure does not prove arbitrary dlopen paths; clean-container GUI and OCR E2E are required.'],
                    'libraries': records, 'elf_files_scanned': len(scanned)}
        manifest['attribution_hashes'] = {str(p.relative_to(dest)): sha256(p) for p in sorted(dest.rglob('*')) if p.is_file() and 'licenses' in p.relative_to(dest).parts}
        (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        verify_linux_libraries(root)
        return manifest
    except Exception:
        # Leave partial evidence for diagnosis, never present an incomplete manifest.
        (dest / 'manifest.json').unlink(missing_ok=True)
        raise


def verify_linux_libraries(stage_root: Path) -> dict:
    root = Path(stage_root).resolve(strict=True)
    dest = root / 'native-libs'
    if dest.is_symlink() or not dest.resolve().is_relative_to(root):
        raise ValueError('Unsafe native library directory')
    manifest = json.loads((dest / 'manifest.json').read_text())
    for name, record in manifest['libraries'].items():
        path = dest / safe_name(name)
        if path.is_symlink() or sha256(path) != record['sha256']:
            raise ValueError(f'Native library integrity failure: {name}')
    for name, digest in manifest['attribution_hashes'].items():
        path = dest / name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.resolve().is_relative_to(dest) or path.is_symlink() or sha256(path) != digest:
            raise ValueError(f'Attribution integrity failure: {name}')
    expected = {'manifest.json', *manifest['libraries'], *manifest['attribution_hashes']}
    for parent, dirs, files in os.walk(dest, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f'Native library integrity failure: symlink {path}')
        for name in files:
            path = Path(parent) / name
            if path.relative_to(dest).as_posix() not in expected:
                raise ValueError(f'Unexpected native library file: {path}')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage_root', type=Path)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    result = verify_linux_libraries(args.stage_root) if args.verify else bundle_linux_libraries(args.stage_root)
    print(json.dumps(result, indent=2))
