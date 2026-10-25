"""Select installed x64 MSVC tools without outrunning the pinned VC runtime.

No installers, privilege changes, runtime copies, or license acceptance. The CI
environment export is build-side only; actual package loader tests remain gates.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent
ENV_KEYS = {
    'PATH', 'INCLUDE', 'LIB', 'LIBPATH', 'VSINSTALLDIR', 'VCINSTALLDIR',
    'VCTOOLSINSTALLDIR', 'VCTOOLSVERSION', 'VSCMD_ARG_TGT_ARCH',
    'VSCMD_ARG_HOST_ARCH', 'WINDOWSSDKDIR', 'WINDOWSSDKVERSION',
    'WINDOWSSDKBINPATH', 'WINDOWSSDKVERBINPATH', 'UNIVERSALCRTSDKDIR',
    'UCRTVERSION', 'WINDOWSLIBPATH', 'VSCMD_VER', 'DEVENVDIR', 'CC', 'CXX',
    'CMAKE_GENERATOR',
}


def version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', value):
        raise ValueError('Invalid numeric tool/runtime version: ' + value)
    parts = tuple(map(int, value.split('.')))
    return parts + (0,) * (4 - len(parts))


def file_version(path):
    """Read numeric PE file version, independent of localized command banners."""
    from ctypes import wintypes
    dll = ctypes.WinDLL('version', use_last_error=True)
    dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    dll.GetFileVersionInfoW.restype = wintypes.BOOL
    dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    dll.VerQueryValueW.restype = wintypes.BOOL
    size = dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ctypes.WinError(ctypes.get_last_error())
    data = ctypes.create_string_buffer(size)
    if not dll.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not dll.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    # VS_FIXEDFILEINFO starts with signature, structure version, file MS/LS.
    fields = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    if length.value < 52 or fields[0] != 0xFEEF04BD:
        raise RuntimeError('Invalid PE version resource: ' + str(path))
    return '.'.join(map(str, (fields[2] >> 16, fields[2] & 0xffff,
                             fields[3] >> 16, fields[3] & 0xffff)))


def crt_version(text):
    values = []
    for field in ('MAJOR', 'MINOR', 'BUILD', 'RBUILD'):
        matches = re.findall(r'^\s*#\s*define\s+_VC_CRT_' + field + r'_VERSION\s+(\d+)\s*(?://.*)?$', text, re.M)
        if len(matches) != 1:
            raise RuntimeError('Cannot establish CRT version from installed crtversion.h: ' + field)
        values.append(matches[0])
    return '.'.join(values)


def check_versions(toolset, compiler, linker, crt, runtime):
    ceiling = version(runtime)
    tools, cl, link = version(toolset), version(compiler), version(linker)
    if (tools[:2] != (14, 44) or cl[:2] != (19, 44) or link[:2] != (14, 44)
            or version(crt)[:2] != (14, 44)):
        raise RuntimeError('Require reviewed MSVC v143/14.44 tools, not an implicit latest toolchain')
    # Compiler and CRT servicing patches can differ. Use the CRT's own version,
    # not a synthetic 19.xx -> 14.xx compiler-patch "minimum runtime" mapping.
    if version(crt) > ceiling:
        raise RuntimeError(f'MSVC toolset {toolset} has CRT headers {crt}, newer than '
                           f'pinned VC runtime {runtime}; review a compatible installed '
                           'toolset or an official runtime pin/hash/license update before building')


def inspect_toolset(directory):
    directory = Path(directory).resolve(strict=True)
    binary = directory / 'bin/Hostx64/x64'
    compiler, linker = binary / 'cl.exe', binary / 'link.exe'
    record = {'toolset': directory.name, 'directory': str(directory),
              'compiler': {'path': str(compiler), 'version': file_version(compiler)},
              'linker': {'path': str(linker), 'version': file_version(linker)}}
    header = directory / 'include/crtversion.h'
    record['crt'] = {'version': crt_version(header.read_text(encoding='utf-8-sig')),
                     'header': str(header), 'header_sha256': hashlib.sha256(header.read_bytes()).hexdigest()}
    record['crt']['import_libraries'] = [
        {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (directory / 'lib/x64/vcruntime.lib', directory / 'lib/x64/msvcprt.lib')]
    return record


def pinned_build_tools():
    pins = json.loads((ROOT / 'build-tool-pins.json').read_text(encoding='utf-8'))
    records = {}
    for name in ('cmake', 'ninja'):
        path = shutil.which(name)
        if not path:
            raise RuntimeError('Install native/ocr/requirements-build.txt first; missing ' + name)
        output = subprocess.check_output([path, '--version'], text=True, timeout=30).splitlines()[0]
        actual = output.removeprefix('cmake version ').strip()
        allowed = {pins[name]['version']}
        if name == 'ninja':
            # The hash-pinned 1.13.0 PyPI wheels contain Kitware's jobserver patch.
            allowed.add(pins[name]['version'] + '.git.kitware.jobserver-pipe-1')
        if actual not in allowed:
            raise RuntimeError(f'{name} version {actual} differs from pinned {pins[name]["version"]}: {path}')
        records[name] = {'path': str(Path(path).resolve()), 'version': actual,
                         'package_version': pins[name]['version']}
    return records


def parse_environment(output):
    result = {}
    for line in output.splitlines():
        name, separator, value = line.partition('=')
        if separator and name.upper() in ENV_KEYS:
            result[name.upper()] = value
    return result


def validate_environment(environment, runtime):
    env = {key.upper(): value for key, value in environment.items()}
    if any(env.get(key) != 'x64' for key in ('VSCMD_ARG_TGT_ARCH', 'VSCMD_ARG_HOST_ARCH')):
        raise RuntimeError('Initialize the native x64 MSVC developer environment with windows_toolchain.py')
    if not all(env.get(key) for key in ('VCTOOLSINSTALLDIR', 'VCTOOLSVERSION', 'INCLUDE', 'LIB', 'LIBPATH', 'WINDOWSSDKVERSION')):
        raise RuntimeError('Incomplete MSVC developer environment')
    record = inspect_toolset(env['VCTOOLSINSTALLDIR'])
    check_versions(record['toolset'], record['compiler']['version'], record['linker']['version'],
                   record['crt']['version'], runtime)
    if version(env['VCTOOLSVERSION'].rstrip('\\/')) != version(record['toolset']):
        raise RuntimeError('VCToolsVersion differs from selected compiler directory')
    for executable, field in (('cl.exe', 'compiler'), ('link.exe', 'linker')):
        found = shutil.which(executable, path=env.get('PATH'))
        if not found or Path(found).resolve() != Path(record[field]['path']):
            raise RuntimeError('Developer PATH selects a different ' + executable + ': ' + str(found))
    for key, expected in [('INCLUDE', record['crt']['header']),
                          *[('LIB', row['path']) for row in record['crt']['import_libraries']]]:
        expected = Path(expected)
        found = next((Path(directory) / expected.name for directory in env[key].split(';')
                      if directory and (Path(directory) / expected.name).is_file()), None)
        if found is None or found.resolve() != expected:
            raise RuntimeError(key + ' does not select the reviewed CRT input: ' + str(expected))
    record['windows_sdk'] = env['WINDOWSSDKVERSION']
    record['runtime_ceiling'] = runtime
    record['runtime_linkage'] = 'MultiThreadedDLL (/MD)'
    record['loader_compatibility'] = 'Pending actual worker/ORT PE import-symbol and loaded-DLL package gates'
    return record


def prepare(report):
    if os.name != 'nt':
        raise RuntimeError('MSVC preparation runs only on the authorized native Windows CI runner')
    runtime = json.loads((ROOT / 'vc-runtime-manifest.json').read_text(encoding='utf-8'))['version']
    report.update(runtime_ceiling=runtime, generator='Ninja', candidates=[])
    build_tools = pinned_build_tools()  # Capture pip-pinned tools before VsDevCmd changes PATH.
    report['build_tools'] = build_tools
    vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    instances = json.loads(subprocess.check_output([
        str(vswhere), '-all', '-products', '*', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json', '-utf8',
    ], encoding='utf-8', timeout=60))
    eligible = []
    for instance in instances:
        installation = Path(instance['installationPath'])
        for directory in sorted((installation / 'VC/Tools/MSVC').glob('*')):
            if not directory.is_dir():
                continue
            row = {'installation': str(installation), 'visual_studio_version': instance['installationVersion'],
                   'toolset': directory.name}
            try:
                row.update(inspect_toolset(directory))
                check_versions(row['toolset'], row['compiler']['version'], row['linker']['version'],
                               row['crt']['version'], runtime)
                if not (installation / 'Common7/Tools/VsDevCmd.bat').is_file():
                    raise RuntimeError('VsDevCmd.bat missing')
                row['compatible'] = True
                eligible.append(row)
            except (OSError, ValueError, RuntimeError) as error:
                row.update(compatible=False, reason=str(error))
            report['candidates'].append(row)
    if not eligible:
        raise RuntimeError(f'No installed x64 MSVC v143/14.44 toolchain fits pinned VC runtime {runtime}; '
                           'see candidates in toolchain evidence. No tool installation or runtime update was attempted.')
    selected = max(eligible, key=lambda row: version(row['toolset']))
    report['selected'] = selected
    script = Path(selected['installation']) / 'Common7/Tools/VsDevCmd.bat'
    command = f'call "{script}" -no_logo -arch=x64 -host_arch=x64 -vcvars_ver={selected["toolset"]} >nul && set'
    result = subprocess.run([os.environ['ComSpec'], '/d', '/u', '/s', '/c', command],
                            capture_output=True, check=True, timeout=120)
    env = parse_environment(result.stdout.decode('utf-16-le'))
    # VS also bundles CMake/Ninja: never let its developer PATH shadow pinned wheels.
    env['PATH'] = ';'.join(dict.fromkeys(str(Path(row['path']).parent) for row in build_tools.values())) + ';' + env.get('PATH', '')
    env.update(CC=selected['compiler']['path'], CXX=selected['compiler']['path'], CMAKE_GENERATOR='Ninja')
    report['active'] = validate_environment(env, runtime)
    return env


def export_environment(path, environment):
    with Path(path).open('a', encoding='utf-8', newline='\n') as stream:
        for key, value in sorted(environment.items()):
            if key.upper() not in ENV_KEYS:
                raise ValueError('Refusing unrelated environment export: ' + key)
            delimiter = 'VSE_' + uuid.uuid4().hex
            stream.write(f'{key}<<{delimiter}\n{value}\n{delimiter}\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--github-env', type=Path, required=True)
    args = parser.parse_args()
    report = {'schema_version': 1, 'status': 'failed', 'source_side_only': True,
              'image_os': os.environ.get('ImageOS'), 'image_version': os.environ.get('ImageVersion'),
              'commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
              'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT')}
    try:
        environment = prepare(report)
        export_environment(args.github_env, environment)
        report['status'] = 'passed'
    except Exception as error:
        report['reason'] = str(error)
        raise
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
