import json
import os
import platform
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import scripts.standalone_runtime_probe as probe


def _host_os_image():
    if sys.platform == 'win32':
        return str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/kernel32.dll')
    if sys.platform == 'darwin':
        return '/usr/lib/libSystem.B.dylib'
    return '/usr/lib/libc.so.6'


def stage_probe(root):
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/standalone_runtime_probe.py").write_bytes(Path(probe.__file__).read_bytes())


from scripts.standalone_runtime_probe import audit_environment, library_violation, loaded_images, summarize_reports


@pytest.mark.parametrize('name', ['vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'concrt140.dll', 'msvcr120.dll', 'python312.dll', 'libiomp5md.dll', 'unknown_vendor.dll'])
def test_windows_external_non_os_runtime_rejected(name):
    assert library_violation('C:\\Windows\\System32\\' + name, r'C:\VSE', 'Windows')
    assert library_violation('C:\\VSE\\runtime\\' + name, r'C:\VSE', 'Windows') is None


@pytest.mark.parametrize('name', ['kernel32.dll', 'ucrtbase.dll', 'msvcp_win.dll', 'DWrite.dll', 'api-ms-win-core-file-l1-1-0.dll'])
def test_windows_os_runtime_allowed(name):
    assert library_violation('C:\\Windows\\System32\\' + name, r'C:\VSE', 'Windows') is None
    assert library_violation('C:\\other\\' + name, r'C:\VSE', 'Windows')


def test_windows_case_prefix_and_neighbor_paths():
    assert library_violation(r'\\?\C:\VSE\runtime\VCRUNTIME140.dll', r'c:\vse', 'Windows') is None
    assert library_violation(r'C:\VSE-other\runtime\python312.dll', r'C:\VSE', 'Windows')
    assert library_violation(r'C:\Windows\System32-evil\kernel32.dll', r'C:\VSE', 'Windows')


@pytest.mark.parametrize('path', ['/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation', '/usr/lib/libSystem.B.dylib'])
def test_mac_base_os_allowed(path):
    assert library_violation(path, '/Applications/VSE.app', 'Darwin') is None


@pytest.mark.parametrize('path', ['/opt/homebrew/lib/libomp.dylib', '/usr/local/lib/libpython3.12.dylib', '/Library/Frameworks/Python.framework/Python', '/usr/lib-other/libX.dylib'])
def test_mac_external_dependency_rejected(path):
    assert library_violation(path, '/Applications/VSE.app', 'Darwin')


MACOS_CRYPTEX = '/System/Volumes/Preboot/Cryptexes/OS'
MACOS_CRYPTEX_CASES = [
    # Actual framework paths reported by the packaged Cocoa GUI workload.
    (f'{MACOS_CRYPTEX}/System/Library/Frameworks/{name}.framework/Versions/A/{name}', True)
    for name in ('AuthenticationServices', 'WebKit', 'SafariServices')
] + [
    (f'{MACOS_CRYPTEX}/usr/lib/libobjc-trampolines.dylib', True),
    (f'{MACOS_CRYPTEX}/usr/lib/swift/libswiftWebKit.dylib', True),
    (f'{MACOS_CRYPTEX}/System/Library/../Library/Frameworks/WebKit.framework/WebKit', True),
    (f'{MACOS_CRYPTEX}/tmp/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}/opt/homebrew/lib/libomp.dylib', False),
    (f'{MACOS_CRYPTEX}/usr/local/lib/libpython3.12.dylib', False),
    (f'{MACOS_CRYPTEX}/Users/example/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}/Library/Frameworks/Python.framework/Python', False),
    (f'{MACOS_CRYPTEX}/System/Library-other/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}/usr/lib-other/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}-other/System/Library/libbad.dylib', False),
    ('/System/Volumes/Preboot-other/Cryptexes/OS/System/Library/libbad.dylib', False),
    ('/System/Volumes/Preboot/Cryptexes/Incoming/OS/System/Library/libbad.dylib', False),
    ('/System/Volumes/Data/System/Library/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}/System/Library/../../tmp/libbad.dylib', False),
    (f'{MACOS_CRYPTEX}/usr/lib/../../tmp/libbad.dylib', False),
    ('/opt/homebrew/lib/libomp.dylib', False),
    ('/usr/local/lib/libpython3.12.dylib', False),
    ('/Users/example/libbad.dylib', False),
]


@pytest.mark.parametrize('path,allowed', MACOS_CRYPTEX_CASES)
def test_mac_cryptex_loaded_library_boundaries(path, allowed):
    assert (library_violation(path, '/Applications/VSE.app', 'Darwin') is None) == allowed


@pytest.mark.parametrize('path,allowed', MACOS_CRYPTEX_CASES)
def test_macho_cryptex_dependency_boundaries(tmp_path, monkeypatch, path, allowed):
    exe = tmp_path / 'VideoSubFinderCli'
    exe.touch()
    monkeypatch.setattr(probe, 'macho_load_commands', lambda _: ([path], []))
    if allowed:
        result = probe.audit_macos_executables(tmp_path, tmp_path / 'reports', [exe])
        assert result['executables'][exe.name][exe.name][path] == 'macOS base OS'
    else:
        with pytest.raises(RuntimeError, match='external/missing dependency'):
            probe.audit_macos_executables(tmp_path, tmp_path / 'reports', [exe])


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink resolution fixture')
def test_mac_cryptex_canonical_alias_and_symlink_escape(tmp_path, monkeypatch):
    # Exercise native POSIX resolution without writing to the real OS roots.
    monkeypatch.setattr(probe, 'sys', SimpleNamespace(platform='darwin'))
    cryptex = tmp_path / 'System/Volumes/Preboot/Cryptexes/OS'
    mounted = tmp_path / 'mounted-os'
    frameworks = mounted / 'System/Library/Frameworks'
    frameworks.mkdir(parents=True)
    (mounted / 'usr/lib').mkdir(parents=True)
    cryptex.parent.mkdir(parents=True)
    cryptex.symlink_to(mounted, target_is_directory=True)
    monkeypatch.setattr(probe, 'MACOS_OS_LIBRARY_ROOTS',
                        tuple(str(tmp_path / base.lstrip('/')) for base in probe.MACOS_OS_LIBRARY_ROOTS))
    bundle = str(tmp_path / 'bundle')
    for image in (frameworks / 'WebKit', cryptex / 'System/Library/Frameworks/WebKit'):
        assert library_violation(str(image), bundle, 'Darwin') is None

    outside = tmp_path / 'homebrew'
    (outside / 'nested').mkdir(parents=True)
    (outside / 'libbad.dylib').touch()
    (frameworks / 'escape').symlink_to(outside / 'nested', target_is_directory=True)
    (mounted / 'usr/lib/libbad.dylib').symlink_to(outside / 'libbad.dylib')
    escaped = [cryptex / 'usr/lib/libbad.dylib',
               cryptex / 'System/Library/Frameworks/escape/../libbad.dylib']
    for image in escaped:
        assert library_violation(str(image), bundle, 'Darwin')

    # Both the direct-load and resolved @loader_path audit routes must reject
    # symlinks even when their unexpanded spelling appears to be trusted.
    exe = tmp_path / 'bundle/VideoSubFinderCli'
    exe.parent.mkdir()
    exe.touch()
    dependencies = [str(image) for image in escaped]
    dependencies += ['@loader_path/../' + str(image.relative_to(tmp_path)) for image in escaped]
    monkeypatch.setattr(probe, 'macho_load_commands', lambda _: (dependencies, []))
    with pytest.raises(RuntimeError, match='external/missing dependency'):
        probe.audit_macos_executables(exe.parent, tmp_path / 'reports', [exe])
    result = json.loads((tmp_path / 'reports/macos-native-imports.json').read_text())
    assert len(result['errors']) == len(dependencies)


def test_no_reports_fails_closed(tmp_path):
    stage_probe(tmp_path)
    audit_environment(tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match='No Python process audit'):
        summarize_reports(tmp_path, tmp_path)


def test_current_process_enumerator():
    assert loaded_images()


def test_hook_inherited_by_child_and_detects_host_python(tmp_path):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    stage_probe(bundle)
    evidence = bundle / 'test-results' / 'dependency-isolation'
    env = dict(os.environ, **audit_environment(bundle, evidence))
    subprocess.run([sys.executable, '-c', 'import subprocess,sys; subprocess.run([sys.executable,"-c","import ctypes"],check=True)'], check=True, env=env)
    assert len(list(evidence.glob('process-*.json'))) == 2
    with pytest.raises(RuntimeError, match='external executable'):
        summarize_reports(bundle, evidence)
    result = json.loads((evidence / 'summary.json').read_text())
    assert not result['passed']
    assert 'ctypes' in result['modules_observed']


def test_required_workload_not_seen(tmp_path):
    stage_probe(tmp_path)
    audit_environment(tmp_path, tmp_path)
    report = {'pid': 1, 'system': platform.system(), 'executable': str(tmp_path / 'runtime/bin/python'),
              'prefix': str(tmp_path / 'runtime'), 'images': [_host_os_image()],
              'snapshots': 1, 'probe_errors': [], 'windows_root': os.environ.get('SystemRoot', r'C:\Windows'), 'modules': {}}
    (tmp_path / 'process-1.json').write_text(json.dumps(report))
    assert summarize_reports(tmp_path, tmp_path)['passed']
    with pytest.raises(RuntimeError, match='Required workload was not observed: paddle'):
        summarize_reports(tmp_path, tmp_path, ('paddle',))


def test_abrupt_worker_exit_still_records_loaded_workload(tmp_path):
    stage_probe(tmp_path)
    evidence = tmp_path / 'reports'
    env = dict(os.environ, **audit_environment(tmp_path, evidence))
    subprocess.run([sys.executable, '-c', 'import sqlite3,time,os; time.sleep(0.5); os._exit(0)'], check=True, env=env)
    reports = list(evidence.glob('process-*.json'))
    assert len(reports) == 1
    assert 'sqlite3' in json.loads(reports[0].read_text())['modules']


def _external_report(bundle, evidence, extra_modules=None):
    report = {'pid': 1, 'system': platform.system(), 'executable': str(bundle / 'runtime/bin/python'),
              'prefix': str(bundle / 'runtime'), 'images': [_host_os_image()],
              'snapshots': 1, 'probe_errors': [], 'windows_root': os.environ.get('SystemRoot', r'C:\Windows'), 'modules': {
                  'sitecustomize': str(evidence / 'hook/sitecustomize.py'),
                  'standalone_runtime_probe': str(evidence / 'hook/standalone_runtime_probe.py'),
                  **(extra_modules or {})}}
    (evidence / 'process-1.json').write_text(json.dumps(report))


def test_external_exact_hooks_allowed_without_writing_bundle(tmp_path):
    bundle, evidence = tmp_path / 'app', tmp_path / 'user-cache/evidence'
    stage_probe(bundle)
    before = sorted(str(path.relative_to(bundle)) for path in bundle.rglob('*'))
    audit_environment(bundle, evidence)
    _external_report(bundle, evidence)
    assert summarize_reports(bundle, evidence)['passed']
    assert before == sorted(str(path.relative_to(bundle)) for path in bundle.rglob('*'))
    assert (evidence / 'hook/standalone_runtime_probe.py').read_bytes() == (bundle / 'scripts/standalone_runtime_probe.py').read_bytes()


@pytest.mark.parametrize('name', ['sitecustomize', 'standalone_runtime_probe'])
def test_modified_external_hook_rejected(tmp_path, name):
    bundle, evidence = tmp_path / 'app', tmp_path / 'cache'
    stage_probe(bundle)
    audit_environment(bundle, evidence)
    _external_report(bundle, evidence)
    (evidence / 'hook' / (name + '.py')).write_text('# unexpected contents')
    with pytest.raises(RuntimeError, match='Missing or modified verification hook'):
        summarize_reports(bundle, evidence)


@pytest.mark.parametrize('name,path', [('evil', 'evil.py'), ('sitecustomize', 'hook/other/sitecustomize.py'), ('standalone_runtime_probe', 'standalone_runtime_probe.py')])
def test_other_external_cache_modules_rejected(tmp_path, name, path):
    bundle, evidence = tmp_path / 'app', tmp_path / 'cache'
    stage_probe(bundle)
    audit_environment(bundle, evidence)
    _external_report(bundle, evidence, {name: str(evidence / path)})
    with pytest.raises(RuntimeError, match='External Python module'):
        summarize_reports(bundle, evidence)


def test_audit_refuses_stale_reports(tmp_path):
    stage_probe(tmp_path)
    evidence = tmp_path / 'reports'
    evidence.mkdir()
    (evidence / 'process-123.json').write_text('{}')
    with pytest.raises(RuntimeError, match='fresh report directory'):
        audit_environment(tmp_path, evidence)


def _write_pe(path, imports):
    import struct
    data = bytearray(4096)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', data, 0x86, 1)
    struct.pack_into('<H', data, 0x94, 240)
    struct.pack_into('<H', data, 0x98, 0x20b)
    struct.pack_into('<I', data, 0x98 + 112 + 8, 0x1000)
    section = 0x98 + 240
    struct.pack_into('<IIII', data, section + 8, 0x800, 0x1000, 0x800, 0x200)
    position = 0x400
    for index, name in enumerate(imports):
        encoded = name.encode('ascii') + b'\0'
        data[position:position + len(encoded)] = encoded
        struct.pack_into('<I', data, 0x200 + 20 * index + 12, 0x1000 + position - 0x200)
        position += len(encoded)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_pe_reader_mandatory_imports(tmp_path):
    binary = tmp_path / 'test.exe'
    _write_pe(binary, ['KERNEL32.dll', 'MSVCP140.dll'])
    assert probe.pe_imports(binary) == ['KERNEL32.dll', 'MSVCP140.dll']
    binary.write_bytes(b'MZ')
    with pytest.raises(ValueError, match='Truncated PE'):
        probe.pe_imports(binary)


def test_pe_recursive_runtime_dependencies_must_be_reachable(tmp_path):
    executable = tmp_path / 'vsf/VideoSubFinder.exe'
    runtime_python = tmp_path / 'runtime/python.exe'
    _write_pe(executable, ['kernel32.dll', 'msvcp140.dll'])
    _write_pe(tmp_path / 'runtime/msvcp140.dll', ['vcruntime140.dll'])
    _write_pe(tmp_path / 'unreachable/vcruntime140.dll', ['ucrtbase.dll'])
    reports = tmp_path / 'reports'
    with pytest.raises(RuntimeError, match='unbundled/unreachable DLL: vcruntime140.dll'):
        probe.audit_windows_executables(tmp_path, runtime_python, reports, [executable])
    _write_pe(tmp_path / 'runtime/vcruntime140.dll', ['ucrtbase.dll'])
    result = probe.audit_windows_executables(tmp_path, runtime_python, reports, [executable])
    assert result['passed']
    assert result['executables']['vsf/VideoSubFinder.exe']['runtime/vcruntime140.dll']['ucrtbase.dll'] == 'Windows base OS'


def test_linux_only_glibc_images_allowed_externally():
    assert probe.library_violation('/lib/x86_64-linux-gnu/libc.so.6', '/app', 'Linux') is None
    assert probe.library_violation('/lib/x86_64-linux-gnu/libQt6Core.so.6', '/app', 'Linux')
    assert probe.library_violation('/lib/x86_64-linux-gnu/libnss_systemd.so.2', '/app', 'Linux')
    assert probe.library_violation('/app/native-libs/libstdc++.so.6', '/app', 'Linux') is None


def test_macho_reader_excludes_weak_optional_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: '''Load command 1
          cmd LC_LOAD_DYLIB
         name @rpath/librequired.dylib (offset 24)
Load command 2
          cmd LC_LOAD_WEAK_DYLIB
         name /opt/optional/libunused.dylib (offset 24)
Load command 3
          cmd LC_RPATH
         path @loader_path/lib dir (offset 12)
''')
    assert probe.macho_load_commands(tmp_path / 'executable') == (['@rpath/librequired.dylib'], ['@loader_path/lib dir'])


def test_macho_dependency_paths_and_ancestor_rpaths(tmp_path, monkeypatch):
    exe = tmp_path / 'vsf/VideoSubFinderCli'
    lib = tmp_path / 'vsf/lib/libone.dylib'
    for path in (exe, lib):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    mapping = {exe: (['@rpath/libone.dylib', '/usr/lib/libSystem.B.dylib'], ['@loader_path/lib']),
               lib: (['/opt/homebrew/lib/libbad.dylib'], [])}
    monkeypatch.setattr(probe, 'macho_load_commands', lambda path: mapping[path])
    with pytest.raises(RuntimeError, match='external/missing dependency'):
        probe.audit_macos_executables(tmp_path, tmp_path / 'reports', [exe])
    mapping[lib] = (['@rpath/libone.dylib'], [])  # self-load cycle terminates
    assert probe.audit_macos_executables(tmp_path, tmp_path / 'reports', [exe])['passed']


def test_macho_unreachable_library_not_accepted(tmp_path, monkeypatch):
    exe = tmp_path / 'vsf/VideoSubFinderCli'
    exe.parent.mkdir()
    exe.touch()
    elsewhere = tmp_path / 'somewhere/libone.dylib'
    elsewhere.parent.mkdir()
    elsewhere.touch()
    monkeypatch.setattr(probe, 'macho_load_commands', lambda _: (['@rpath/libone.dylib'], ['@loader_path/lib']))
    with pytest.raises(RuntimeError, match='unresolved native dependency'):
        probe.audit_macos_executables(tmp_path, tmp_path / 'reports', [exe])


def test_windows_native_discovery_in_architecture_directory(tmp_path):
    exe = tmp_path / 'backend/subfinder/windows/x64/VideoSubFinderCli.exe'
    ffmpeg = tmp_path / 'runtime/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64.exe'
    for path in (exe, ffmpeg):
        _write_pe(path, ['kernel32.dll'])
    result = probe.audit_windows_executables(tmp_path, tmp_path / 'runtime/python.exe', tmp_path / 'reports')
    assert len(result['executables']) == 2


def test_macos_native_discovery_in_architecture_directory(tmp_path, monkeypatch):
    exe = tmp_path / 'backend/subfinder/macos/arm64/VideoSubFinderCli'
    ffmpeg = tmp_path / 'runtime/lib/python3.12/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64'
    for path in (exe, ffmpeg):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(probe, 'macho_load_commands', lambda _: (['/usr/lib/libSystem.B.dylib'], []))
    result = probe.audit_macos_executables(tmp_path, tmp_path / 'reports')
    assert len(result['executables']) == 2


@pytest.mark.parametrize('path,system,expected', [
    (r'\\?\C:\VSE\runtime\..\runtime\Python312.dll', 'Windows', r'c:\vse\runtime\python312.dll'),
    (r'\\?\UNC\server\Share\VSE\python.exe', 'Windows', r'\\server\share\vse\python.exe'),
    ('/usr/lib/../lib/libSystem.B.dylib', 'Darwin', '/usr/lib/libSystem.B.dylib'),
    ('/opt/homebrew/lib/libomp.dylib', 'Darwin', '/opt/homebrew/lib/libomp.dylib'),
])
def test_target_path_normalization_never_uses_host_filesystem(path, system, expected, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Target lexical normalization must not use host Path')
    monkeypatch.setattr(probe, 'Path', forbidden)
    assert probe._normal(path, system) == expected
    if system != platform.system():
        assert probe._identity(path, system) == expected


def test_windows_unc_prefix_identity_is_not_a_neighbor_prefix(monkeypatch):
    # Pure UNC boundary fixture: never contact a synthetic SMB server on Windows.
    monkeypatch.setattr(probe, '_identity', probe._normal)
    assert probe._inside(r'\\?\UNC\server\Share\VSE\python.exe', r'\\server\share\vse', 'Windows')
    assert not probe._inside(r'\\?\UNC\server\Share\VSE-other\python.exe', r'\\server\share\vse', 'Windows')


def test_native_canonical_root_identity(tmp_path):
    root = tmp_path / 'bundle with spaces'
    root.mkdir()
    runtime = root / 'runtime'
    runtime.mkdir()
    executable = runtime / 'python.exe'
    executable.touch()
    assert probe._inside(str(executable), str(root), platform.system())
    assert not probe._inside(str(tmp_path / 'outside/python.exe'), str(root), platform.system())


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows path API required')
def test_windows_native_root_case_and_exact_hook_aliases(tmp_path):
    bundle, evidence = tmp_path / 'Bundle With Spaces', tmp_path / 'External Evidence'
    stage_probe(bundle)
    audit_environment(bundle, evidence)
    _external_report(bundle, evidence)
    report_path = evidence / 'process-1.json'
    report = json.loads(report_path.read_text())
    report['executable'] = report['executable'].upper()
    report['prefix'] = report['prefix'].upper()
    report['modules'] = {name: path.upper() for name, path in report['modules'].items()}
    report_path.write_text(json.dumps(report))
    assert summarize_reports(bundle, evidence)['passed']


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows 8.3 path API required')
def test_windows_native_short_path_identity(tmp_path):
    import ctypes
    from ctypes import wintypes
    root = tmp_path / 'Bundle With A Long Name'
    root.mkdir()
    executable = root / 'Bundled Python Executable.exe'
    executable.touch()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    short_path = kernel.GetShortPathNameW
    short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    short_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = short_path(str(executable), buffer, len(buffer))
    assert length and length < len(buffer), ctypes.get_last_error()
    if buffer.value.lower() == str(executable).lower():
        pytest.skip('This Windows volume has 8.3 aliases disabled')
    assert probe._inside(buffer.value, str(root), 'Windows')
    assert probe._identity(buffer.value, 'Windows') == probe._identity(str(executable), 'Windows')
