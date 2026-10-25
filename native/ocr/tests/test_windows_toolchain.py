"""Pure toolchain policy tests plus Windows-only cmd fixtures; no downloads."""
import json
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import build
import build_opencv
import windows_toolchain as toolchain

RUNTIME = '14.44.35211.0'
# Deliberately not a generic path word (macOS legitimately uses /private/var).
LEAK_SENTINEL = 'VSE_TEST_ONLY_SECRET_8fa6be7d4e2c901b'


def fixture_executables(tmp_path):
    """Use native canonical paths, not POSIX-rooted strings on Windows drives."""
    root = tmp_path / 'Pinned Build Tools with spaces'
    root.mkdir()
    paths = {}
    for name in ('cmake', 'ninja', 'cl'):
        path = root / (name + '.exe')
        path.write_text('test fixture; never executed')
        paths[name] = str(path.resolve(strict=True))
        assert Path(paths[name]).is_absolute()
        assert Path(paths[name]).resolve(strict=True) == Path(paths[name])
    return paths


def header(version=RUNTIME):
    return '\n'.join(f'#define _VC_CRT_{field}_VERSION {value}'
                     for field, value in zip(('MAJOR', 'MINOR', 'BUILD', 'RBUILD'), version.split('.')))


def fixture_tools(tmp_path, crt=RUNTIME):
    installation = tmp_path / 'Visual Studio with spaces'
    directory = installation / 'VC/Tools/MSVC/14.44.35207'
    for relative in ('bin/Hostx64/x64/cl.exe', 'bin/Hostx64/x64/link.exe',
                     'lib/x64/vcruntime.lib', 'lib/x64/msvcprt.lib', 'include/crtversion.h'):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header(crt) if path.name == 'crtversion.h' else 'test tool input')
    script = installation / 'Common7/Tools/VsDevCmd.bat'
    script.parent.mkdir(parents=True)
    script.write_text('test fixture; never executed')
    environment = {'VSCMD_ARG_TGT_ARCH': 'x64', 'VSCMD_ARG_HOST_ARCH': 'x64',
                   'VCTOOLSINSTALLDIR': str(directory), 'VCTOOLSVERSION': directory.name,
                   'INCLUDE': str(directory / 'include'), 'LIB': str(directory / 'lib/x64'),
                   'LIBPATH': str(directory / 'lib/x64'), 'WINDOWSSDKVERSION': '10.0.26100.0\\',
                   'PATH': str(directory / 'bin/Hostx64/x64')}
    return installation, directory, environment


def file_version(path):
    # Compiler/linker patches intentionally differ from the CRT version.
    return '19.44.35299.7' if Path(path).name == 'cl.exe' else '14.44.35299.7'


def test_compiler_patch_is_not_a_synthetic_crt_minimum():
    toolchain.check_versions('14.44.35207', '19.44.35299.7', '14.44.35299.7', RUNTIME, RUNTIME)
    toolchain.check_versions('14.44.35207', '19.44.35299.7', '14.44.35299.7', '14.44.35207.0', RUNTIME)


def test_actual_server_header_needs_updated_app_local_runtime_not_gate_relaxation():
    runtime = json.loads((Path(toolchain.__file__).parent / 'vc-runtime-manifest.json').read_text())['version']
    assert runtime == '14.51.36247.0'
    observed = ('14.44.35207', '19.44.35222.0', '14.44.35222.0', '14.44.35220.0')
    with pytest.raises(RuntimeError, match='newer than pinned VC runtime'):
        toolchain.check_versions(*observed, '14.44.35211.0')
    toolchain.check_versions(*observed, runtime)
    with pytest.raises(RuntimeError, match='Require reviewed MSVC'):
        toolchain.check_versions('14.51.36247', '19.51.36247.0', runtime, runtime, runtime)


@pytest.mark.parametrize('versions', [
    ('14.50.35717', '19.50.35717.0', '14.50.35717.0', '14.50.35717.0'),
    ('14.44.35207', '19.50.35717.0', '14.44.35211.0', RUNTIME),
    ('14.44.35207', '19.44.35211.0', '14.50.35717.0', RUNTIME),
    ('14.38.33130', '19.38.33130.0', '14.38.33130.0', '14.38.33130.0'),
    ('14.44.35207', '19.44.35211.0', '14.44.35211.0', '14.44.35212.0'),
    ('14.44.35207', '19.44.35211.0', '14.44.35211.0', '14.44.35211.1'),
])
def test_unreviewed_family_or_newer_crt_fails_closed(versions):
    with pytest.raises(RuntimeError):
        toolchain.check_versions(*versions, RUNTIME)


def test_crt_macro_parser_requires_complete_unambiguous_numeric_evidence():
    assert toolchain.crt_version(header()) == RUNTIME
    for bad in ('', header().replace('35211', 'NEW_BUILD'), header() + '\n' + header()):
        with pytest.raises(RuntimeError, match='Cannot establish CRT version'):
            toolchain.crt_version(bad)


def test_active_environment_binds_paths_header_libraries_and_versions(tmp_path):
    _, directory, environment = fixture_tools(tmp_path)
    with patch.object(toolchain, 'file_version', side_effect=file_version), \
            patch.object(toolchain.shutil, 'which', side_effect=lambda name, **_: str(directory / 'bin/Hostx64/x64' / name)):
        result = toolchain.validate_environment(environment, RUNTIME)
    assert result['crt']['version'] == RUNTIME
    assert len(result['crt']['header_sha256']) == 64
    assert len(result['crt']['import_libraries']) == 2
    assert result['compiler']['version'] == '19.44.35299.7'
    assert result['runtime_linkage'] == 'MultiThreadedDLL (/MD)'
    assert result['loader_compatibility'].startswith('Pending')


@pytest.mark.parametrize('change', ['architecture', 'compiler', 'header', 'library', 'toolset'])
def test_active_environment_rejects_shadowed_or_inconsistent_tools(tmp_path, change):
    _, directory, environment = fixture_tools(tmp_path)
    alternate = tmp_path / 'unrelated'; alternate.mkdir()
    if change == 'architecture':
        environment['VSCMD_ARG_TGT_ARCH'] = 'x86'
    elif change == 'toolset':
        environment['VCTOOLSVERSION'] = '14.44.35208'
    elif change in ('header', 'library'):
        name, key = ('crtversion.h', 'INCLUDE') if change == 'header' else ('vcruntime.lib', 'LIB')
        (alternate / name).write_text('unrelated')
        environment[key] = str(alternate) + ';' + environment[key]
    def which(name, **_):
        return str(alternate / name if change == 'compiler' else directory / 'bin/Hostx64/x64' / name)
    with patch.object(toolchain, 'file_version', side_effect=file_version), \
            patch.object(toolchain.shutil, 'which', side_effect=which), pytest.raises(RuntimeError):
        toolchain.validate_environment(environment, RUNTIME)


def test_prepare_discovers_installed_version_and_preserves_pinned_build_tools(tmp_path):
    installation, directory, environment = fixture_tools(tmp_path)
    fake_os = SimpleNamespace(name='nt', environ={'ProgramFiles(x86)': str(tmp_path), 'ComSpec': 'cmd.exe'})
    paths = fixture_executables(tmp_path)
    build_tools = {name: {'path': paths[name], 'version': value}
                   for name, value in [('cmake', '4.1.2'), ('ninja', '1.13.0')]}
    discovery = json.dumps([{'installationPath': str(installation), 'installationVersion': '17.14.fixture'}])
    def initialize(command, **kwargs):
        marker = re.search(r'echo (VSE_TOOLCHAIN_ENV_[0-9a-f]+)&&', command)[1]
        env_text = '\r\n'.join(key + '=' + value for key, value in environment.items())
        stdout = ('initializing exact toolset\r\n' + marker + '\r\n' + env_text +
                  '\r\nSECRET_TOKEN=' + LEAK_SENTINEL).encode('utf-16-le')
        return SimpleNamespace(stdout=stdout, stderr=b'', returncode=0)
    report = {}
    with patch.object(toolchain, 'os', fake_os), \
            patch.object(toolchain, 'pinned_build_tools', return_value=build_tools), \
            patch.object(toolchain, 'file_version', side_effect=file_version), \
            patch.object(toolchain.subprocess, 'check_output', return_value=discovery), \
            patch.object(toolchain.subprocess, 'run', side_effect=initialize) as run, \
            patch.object(toolchain.shutil, 'which', side_effect=lambda name, **_: str(directory / 'bin/Hostx64/x64' / name)):
        result = toolchain.prepare(report)
    command = run.call_args.args[0]
    assert isinstance(command, str)
    assert command.startswith('"cmd.exe" /d /q /u /s /v:off /c "call "')
    assert '-arch=x64 -host_arch=x64 -vcvars_ver=14.44.35207' in command
    assert '\\"' not in command and '>nul' not in command
    assert run.call_args.kwargs['executable'] == 'cmd.exe'
    assert run.call_args.kwargs['shell'] is False
    assert run.call_args.kwargs['check'] is False
    assert result['PATH'].startswith(str(Path(paths['cmake']).parent) + ';')
    assert result['CMAKE_GENERATOR'] == 'Ninja'
    assert report['selected']['crt']['version'] == RUNTIME
    assert report['initialization']['stdout_diagnostics'] == 'initializing exact toolset'
    assert report['initialization']['returncode'] == 0
    assert LEAK_SENTINEL not in json.dumps(report)
    assert 'SECRET_TOKEN' not in result


def test_missing_compatible_installed_tools_records_mismatch_before_any_command(tmp_path):
    installation, _, _ = fixture_tools(tmp_path, crt='14.44.35212.0')
    # Exercise the old ceiling independently of the reviewed current runtime pin.
    pins = tmp_path / 'pins'; pins.mkdir()
    (pins / 'vc-runtime-manifest.json').write_text(json.dumps({'version': RUNTIME}))
    fake_os = SimpleNamespace(name='nt', environ={'ProgramFiles(x86)': str(tmp_path)})
    discovery = json.dumps([{'installationPath': str(installation), 'installationVersion': '17.14.fixture'}])
    report = {}
    with patch.object(toolchain, 'ROOT', pins), patch.object(toolchain, 'os', fake_os), patch.object(toolchain, 'pinned_build_tools', return_value={}), \
            patch.object(toolchain, 'file_version', side_effect=file_version), \
            patch.object(toolchain.subprocess, 'check_output', return_value=discovery), \
            patch.object(toolchain.subprocess, 'run') as run, pytest.raises(RuntimeError, match='No installed'):
        toolchain.prepare(report)
    run.assert_not_called()
    assert not report['candidates'][0]['compatible']
    assert report['candidates'][0]['crt']['version'] == '14.44.35212.0'
    assert '14.44.35211.0' in report['candidates'][0]['reason']


def test_environment_exports_only_required_build_values(tmp_path):
    environment = toolchain.parse_environment('Path=C:\\A=B\nLIB=x\nSECRET_TOKEN=never-export\n=C:=ignored\n')
    assert environment == {'PATH': 'C:\\A=B', 'LIB': 'x'}
    output = tmp_path / 'github-env'
    toolchain.export_environment(output, environment)
    assert 'PATH<<VSE_' in output.read_text()
    assert 'SECRET' not in output.read_text()
    with pytest.raises(ValueError, match='unrelated'):
        toolchain.export_environment(output, {'SECRET_TOKEN': 'must not export'})


def test_build_tool_versions_must_match_committed_wheels(tmp_path):
    paths = fixture_executables(tmp_path)
    with patch.object(toolchain.shutil, 'which', return_value=paths['cmake']), \
            patch.object(toolchain.subprocess, 'check_output', return_value='cmake version 3.31.6\n'), \
            pytest.raises(RuntimeError, match='differs from pinned 4.1.2'):
        toolchain.pinned_build_tools()


def test_pinned_ninja_wheel_jobserver_suffix_is_not_version_drift(tmp_path):
    paths = fixture_executables(tmp_path)
    with patch.object(toolchain.shutil, 'which', side_effect=paths.__getitem__), \
            patch.object(toolchain.subprocess, 'check_output', side_effect=[
                'cmake version 4.1.2\n', '1.13.0.git.kitware.jobserver-pipe-1\n']):
        result = toolchain.pinned_build_tools()
    assert result['ninja']['package_version'] == '1.13.0'
    assert result['ninja']['version'] == '1.13.0.git.kitware.jobserver-pipe-1'
    assert result['ninja']['path'] == paths['ninja']
    assert result['cmake']['path'] == paths['cmake']


def test_opencv_explicit_ninja_and_shared_crt_for_static_libraries(tmp_path):
    paths = fixture_executables(tmp_path)
    with patch.object(build_opencv.platform, 'system', return_value='Windows'), \
            patch.object(toolchain, 'validate_environment', return_value={'compiler': {'path': paths['cl']}}), \
            patch.object(toolchain, 'pinned_build_tools', return_value={'ninja': {'path': paths['ninja']}}), \
            patch.object(build_opencv.subprocess, 'run') as run:
        build_opencv.build(tmp_path / 'src', tmp_path / 'build', tmp_path / 'prefix', generator='Ninja')
    command = run.call_args_list[0].args[0]
    assert command[command.index('-G') + 1] == 'Ninja'
    for flag in ('-DBUILD_WITH_STATIC_CRT=OFF', '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL',
                 '-DCMAKE_C_COMPILER=' + paths['cl'], '-DCMAKE_CXX_COMPILER=' + paths['cl'],
                 '-DCMAKE_MAKE_PROGRAM=' + paths['ninja']):
        assert flag in command


def test_windows_build_requires_explicit_ninja_before_model_or_network_work():
    args = SimpleNamespace(platform='windows', arch='x64', runner_test_only=True,
                           acknowledge_vc_redistribution_license=False, vc_runtime_dir=None,
                           vc_license=None, generator=None)
    with patch.object(build, 'native_target'), pytest.raises(ValueError, match='--generator Ninja'):
        build.build(args)


@pytest.mark.parametrize('matching_cmake', [True, False])
def test_worker_configuration_uses_the_same_verified_compiler_ninja_and_dll_crt(tmp_path, monkeypatch, matching_cmake):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    paths = fixture_executables(tmp_path)
    alternate_cmake = tmp_path / 'Unverified CMake.exe'
    alternate_cmake.write_text('another fixture; never executed')
    args = SimpleNamespace(platform='windows', arch='x64', runner_test_only=True,
                           acknowledge_vc_redistribution_license=False, vc_runtime_dir=None,
                           vc_license=None, generator='Ninja', models=tmp_path, output=tmp_path / 'output',
                           cmake='cmake', work_dir=tmp_path / 'work', cache=tmp_path / 'cache', offline=True, jobs=3)
    build_tools = {name: {'path': paths[name]} for name in ('cmake', 'ninja')}
    class StopAfterConfigure(Exception):
        pass
    with patch.object(build, 'native_target'), patch.object(build, 'verify_models', return_value=[]), \
            patch.object(toolchain, 'validate_environment', return_value={'compiler': {'path': paths['cl']}}), \
            patch.object(toolchain, 'pinned_build_tools', return_value=build_tools), \
            patch.object(build.shutil, 'which', return_value=paths['cmake'] if matching_cmake else str(alternate_cmake.resolve(strict=True))), \
            patch.object(build, 'fetch_sources', return_value=tmp_path / 'source'), \
            patch.object(build, 'fetch_ort', return_value=tmp_path / 'sdk'), \
            patch.object(build, 'build_opencv') as opencv, \
            patch.object(build, 'opencv_config', return_value=tmp_path / 'opencv'), \
            patch.object(build.subprocess, 'run', side_effect=StopAfterConfigure) as run, \
            pytest.raises(StopAfterConfigure if matching_cmake else ValueError,
                          match=None if matching_cmake else '--cmake must select the verified pinned CMake executable'):
        build.build(args)
    if not matching_cmake:
        opencv.assert_not_called()
        run.assert_not_called()
        return
    assert opencv.call_args.args[-1] == 'Ninja'
    command = run.call_args.args[0]
    assert command[command.index('-G') + 1] == 'Ninja'
    assert '-DCMAKE_CXX_COMPILER=' + paths['cl'] in command
    assert '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL' in command
    assert '-DCMAKE_MAKE_PROGRAM=' + paths['ninja'] in command


def test_workflow_initializes_both_server_toolchains_and_records_outcome():
    root = Path(toolchain.__file__).resolve().parents[2]
    text = (root / '.github/workflows/native-ocr.yml').read_text(encoding='utf-8')
    assert 'os: windows-2022' in text and 'os: windows-2025' in text
    assert text.index('id: windows-toolchain') < text.index('id: native-build')
    assert '--github-env "$env:GITHUB_ENV"' in text
    assert "'--generator Ninja'" in text
    assert '"native-toolchain":"${{ steps.windows-toolchain.outcome }}"' in text


@pytest.mark.parametrize('installation', [r'C:\Program Files\Microsoft Visual Studio\18\Enterprise',
                                         r'C:\Program Files (x86)\VS 2022\Enterprise',
                                         r'C:\Build Tools & SDK (reviewed)\VS'])
def test_cmd_uses_raw_quoted_batch_path_not_crt_argv_serialization(installation):
    script = installation + r'\Common7\Tools\VsDevCmd.bat'
    comspec = r'C:\Windows\System32\cmd.exe'
    marker = 'VSE_TOOLCHAIN_ENV_' + 'a' * 32
    command = toolchain.initialization_command(comspec, script, '14.44.35207', marker)
    assert command == (f'"{comspec}" /d /q /u /s /v:off /c "call "{script}" '
                       f'-no_logo -arch=x64 -host_arch=x64 -vcvars_ver=14.44.35207 '
                       f'&& echo {marker}&& set"')
    # This is the exact Windows serialization of the previous (incorrect) list.
    previous = subprocess.list2cmdline([comspec, '/d', '/u', '/s', '/c',
                                       f'call "{script}" -vcvars_ver=14.44.35207 >nul && set'])
    assert '\\"' in previous and '\\"' not in command


@pytest.mark.parametrize('bad', ['quote"', 'expand%PATH%', 'expand!PATH!', 'caret^', 'new\nline', 'pipe|'])
def test_cmd_rejects_untrusted_expansion_and_control_characters(bad):
    with pytest.raises(ValueError):
        toolchain.initialization_command('cmd.exe', bad, '14.44.35207', 'VSE_TOOLCHAIN_ENV_' + 'a' * 32)


def test_failed_initialization_preserves_diagnostics_without_environment_secrets(tmp_path):
    # Legitimate paths containing these words must not trip leak assertions.
    installation, _, _ = fixture_tools(tmp_path / 'private' / 'var' / 'secret')
    selected = {'installation': str(installation), 'toolset': '14.44.35207'}
    report = {}
    failed = SimpleNamespace(returncode=7, stdout=('[ERROR] Could not initialize VC\r\nPRIVATE_TOKEN=' + LEAK_SENTINEL).encode('utf-16-le'),
                             stderr=('initializer diagnostic: ' + LEAK_SENTINEL).encode())
    with patch.object(toolchain, 'os', SimpleNamespace(environ={'ComSpec': 'cmd.exe', 'PRIVATE_TOKEN': LEAK_SENTINEL})), \
            patch.object(toolchain.subprocess, 'run', return_value=failed), \
            pytest.raises(RuntimeError, match='exit 7'):
        toolchain.initialize_environment(selected, report)
    assert report['initialization']['returncode'] == 7
    assert 'Could not initialize VC' in report['initialization']['stdout_diagnostics']
    assert LEAK_SENTINEL not in json.dumps(report)
    assert report['initialization']['script'] == str(installation / 'Common7/Tools/VsDevCmd.bat')
    assert 'PRIVATE_TOKEN=' not in json.dumps(report)
    assert len(report['initialization']['script_sha256']) == 64


@pytest.mark.parametrize('line', [f'CHILD_ONLY={LEAK_SENTINEL}', f'set CHILD_ONLY={LEAK_SENTINEL}',
                                 f'set "CHILD_ONLY={LEAK_SENTINEL}"', f'@set "CHILD_ONLY={LEAK_SENTINEL}"'])
def test_initialization_diagnostics_omit_even_child_created_quoted_assignments(line):
    with patch.object(toolchain, 'os', SimpleNamespace(environ={})):
        assert toolchain.diagnostic_text(line.encode('utf-16-le')) == '[environment assignment omitted]'


def test_initialization_timeout_preserves_prefix_but_not_partial_environment(tmp_path):
    installation, _, _ = fixture_tools(tmp_path / 'private' / 'var' / 'secret')
    report = {}
    def timeout(command, **kwargs):
        marker = re.search(r'echo (VSE_TOOLCHAIN_ENV_[0-9a-f]+)&&', command)[1]
        data = ('still initializing\r\n' + marker + '\r\nSECRET_TOKEN=' + LEAK_SENTINEL).encode('utf-16-le')
        raise subprocess.TimeoutExpired(command, 120, output=data, stderr=b'timeout diagnostic')
    with patch.object(toolchain, 'os', SimpleNamespace(environ={'ComSpec': 'cmd.exe'})), \
            patch.object(toolchain.subprocess, 'run', side_effect=timeout), \
            pytest.raises(RuntimeError, match='timed out'):
        toolchain.initialize_environment({'installation': str(installation), 'toolset': '14.44.35207'}, report)
    assert report['initialization']['timed_out']
    assert report['initialization']['stdout_diagnostics'] == 'still initializing'
    assert LEAK_SENTINEL not in json.dumps(report)
    assert report['initialization']['script'] == str(installation / 'Common7/Tools/VsDevCmd.bat')


def test_success_without_delimiter_never_persists_unknown_output(tmp_path):
    installation, _, _ = fixture_tools(tmp_path)
    report = {}
    result = SimpleNamespace(returncode=0, stdout=LEAK_SENTINEL.encode(), stderr=b'')
    with patch.object(toolchain, 'os', SimpleNamespace(environ={'ComSpec': 'cmd.exe'})), \
            patch.object(toolchain.subprocess, 'run', return_value=result), \
            pytest.raises(RuntimeError, match='delimiter'):
        toolchain.initialize_environment({'installation': str(installation), 'toolset': '14.44.35207'}, report)
    assert LEAK_SENTINEL not in json.dumps(report)


@pytest.mark.parametrize('changed', ['toolset', 'directory', 'compiler', 'linker', 'crt'])
def test_active_candidate_must_match_exact_preselected_snapshot(changed):
    selected = {name: {'path': name, 'sha256': 'original'} for name in ('compiler', 'linker', 'crt')}
    selected.update(toolset='14.44.35207', directory='reviewed installation')
    toolchain.bind_selected_toolset(dict(selected), selected)
    active = dict(selected)
    active[changed] = 'another same-family toolset or changed input'
    with pytest.raises(RuntimeError, match='differs from selected toolset: ' + changed):
        toolchain.bind_selected_toolset(active, selected)


@pytest.mark.skipif(os.name != 'nt', reason='Real cmd.exe execution requires the authorized Windows CI runner')
@pytest.mark.parametrize('returncode', [0, 7])
def test_real_windows_cmd_initialization_with_spaces_ampersand_and_parentheses(tmp_path, returncode):
    # No compiler/installers involved: only exercise real cmd quoting/diagnostics.
    installation = tmp_path / 'Build Tools & SDK (reviewed)'
    script = installation / 'Common7/Tools/VsDevCmd.bat'
    script.parent.mkdir(parents=True)
    include = tmp_path / 'exact header fixture'
    include.mkdir()
    include = str(include.resolve(strict=True))
    script.write_text('@echo off\n'
                      'if not "%*"=="-no_logo -arch=x64 -host_arch=x64 -vcvars_ver=14.44.35207" exit /b 9\n'
                      'echo real fixture initialization\n'
                      f'set "INCLUDE={include}"\n'
                      f'set "PRIVATE_TOOLCHAIN_TEST={LEAK_SENTINEL}"\n'
                      f'exit /b {returncode}\n', encoding='utf-8')
    report = {}
    selected = {'installation': str(installation), 'toolset': '14.44.35207'}
    if returncode:
        with pytest.raises(RuntimeError, match='exit 7'):
            toolchain.initialize_environment(selected, report)
    else:
        env = toolchain.initialize_environment(selected, report)
        assert env['INCLUDE'] == include
        assert 'PRIVATE_TOOLCHAIN_TEST' not in env
    assert report['initialization']['returncode'] == returncode
    assert 'real fixture initialization' in report['initialization']['stdout_diagnostics']
    assert LEAK_SENTINEL not in json.dumps(report)
