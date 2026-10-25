import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import scripts.standalone_runtime_probe as probe


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
    report = {'pid': 1, 'system': 'Darwin', 'executable': str(tmp_path / 'runtime/bin/python'),
              'prefix': str(tmp_path / 'runtime'), 'images': ['/usr/lib/libSystem.B.dylib'],
              'snapshots': 1, 'probe_errors': [], 'windows_root': '', 'modules': {}}
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
    report = {'pid': 1, 'system': 'Darwin', 'executable': str(bundle / 'runtime/bin/python'),
              'prefix': str(bundle / 'runtime'), 'images': ['/usr/lib/libSystem.B.dylib'],
              'snapshots': 1, 'probe_errors': [], 'windows_root': '', 'modules': {
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
