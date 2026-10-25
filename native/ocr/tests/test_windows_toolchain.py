"""Windows toolchain policy/command tests; no Windows execution or downloads."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import build
import build_opencv
import windows_toolchain as toolchain

RUNTIME = '14.44.35211.0'


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
    build_tools = {name: {'path': str(tmp_path / 'Pinned Python Scripts' / (name + '.exe')), 'version': value}
                   for name, value in [('cmake', '4.1.2'), ('ninja', '1.13.0')]}
    discovery = json.dumps([{'installationPath': str(installation), 'installationVersion': '17.14.fixture'}])
    env_bytes = '\r\n'.join(key + '=' + value for key, value in environment.items()).encode('utf-16-le')
    report = {}
    with patch.object(toolchain, 'os', fake_os), \
            patch.object(toolchain, 'pinned_build_tools', return_value=build_tools), \
            patch.object(toolchain, 'file_version', side_effect=file_version), \
            patch.object(toolchain.subprocess, 'check_output', return_value=discovery), \
            patch.object(toolchain.subprocess, 'run', return_value=SimpleNamespace(stdout=env_bytes)) as run, \
            patch.object(toolchain.shutil, 'which', side_effect=lambda name, **_: str(directory / 'bin/Hostx64/x64' / name)):
        result = toolchain.prepare(report)
    command = run.call_args.args[0]
    assert command[:5] == ['cmd.exe', '/d', '/u', '/s', '/c']
    assert '-arch=x64 -host_arch=x64 -vcvars_ver=14.44.35207' in command[-1]
    assert 'call "' in command[-1] and ' && set' in command[-1]
    assert result['PATH'].startswith(str(tmp_path / 'Pinned Python Scripts') + ';')
    assert result['CMAKE_GENERATOR'] == 'Ninja'
    assert report['selected']['crt']['version'] == RUNTIME


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


def test_build_tool_versions_must_match_committed_wheels():
    with patch.object(toolchain.shutil, 'which', return_value='/installed/cmake'), \
            patch.object(toolchain.subprocess, 'check_output', return_value='cmake version 3.31.6\n'), \
            pytest.raises(RuntimeError, match='differs from pinned 4.1.2'):
        toolchain.pinned_build_tools()


def test_pinned_ninja_wheel_jobserver_suffix_is_not_version_drift():
    with patch.object(toolchain.shutil, 'which', side_effect=lambda name: '/pinned/' + name), \
            patch.object(toolchain.subprocess, 'check_output', side_effect=[
                'cmake version 4.1.2\n', '1.13.0.git.kitware.jobserver-pipe-1\n']):
        result = toolchain.pinned_build_tools()
    assert result['ninja']['package_version'] == '1.13.0'
    assert result['ninja']['version'] == '1.13.0.git.kitware.jobserver-pipe-1'


def test_opencv_explicit_ninja_and_shared_crt_for_static_libraries(tmp_path):
    with patch.object(build_opencv.platform, 'system', return_value='Windows'), \
            patch.object(toolchain, 'validate_environment', return_value={'compiler': {'path': 'C:/verified/cl.exe'}}), \
            patch.object(toolchain, 'pinned_build_tools', return_value={'ninja': {'path': 'C:/pinned/ninja.exe'}}), \
            patch.object(build_opencv.subprocess, 'run') as run:
        build_opencv.build(tmp_path / 'src', tmp_path / 'build', tmp_path / 'prefix', generator='Ninja')
    command = run.call_args_list[0].args[0]
    assert command[command.index('-G') + 1] == 'Ninja'
    for flag in ('-DBUILD_WITH_STATIC_CRT=OFF', '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL',
                 '-DCMAKE_C_COMPILER=C:/verified/cl.exe', '-DCMAKE_CXX_COMPILER=C:/verified/cl.exe',
                 '-DCMAKE_MAKE_PROGRAM=C:/pinned/ninja.exe'):
        assert flag in command


def test_windows_build_requires_explicit_ninja_before_model_or_network_work():
    args = SimpleNamespace(platform='windows', arch='x64', runner_test_only=True,
                           acknowledge_vc_redistribution_license=False, vc_runtime_dir=None,
                           vc_license=None, generator=None)
    with patch.object(build, 'native_target'), pytest.raises(ValueError, match='--generator Ninja'):
        build.build(args)


def test_worker_configuration_uses_the_same_verified_compiler_ninja_and_dll_crt(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    args = SimpleNamespace(platform='windows', arch='x64', runner_test_only=True,
                           acknowledge_vc_redistribution_license=False, vc_runtime_dir=None,
                           vc_license=None, generator='Ninja', models=tmp_path, output=tmp_path / 'output',
                           cmake='cmake', work_dir=tmp_path / 'work', cache=tmp_path / 'cache', offline=True, jobs=3)
    build_tools = {'cmake': {'path': '/pinned/cmake'}, 'ninja': {'path': '/pinned/ninja'}}
    class StopAfterConfigure(Exception):
        pass
    with patch.object(build, 'native_target'), patch.object(build, 'verify_models', return_value=[]), \
            patch.object(toolchain, 'validate_environment', return_value={'compiler': {'path': 'C:/verified/cl.exe'}}), \
            patch.object(toolchain, 'pinned_build_tools', return_value=build_tools), \
            patch.object(build.shutil, 'which', return_value='/pinned/cmake'), \
            patch.object(build, 'fetch_sources', return_value=tmp_path / 'source'), \
            patch.object(build, 'fetch_ort', return_value=tmp_path / 'sdk'), \
            patch.object(build, 'build_opencv') as opencv, \
            patch.object(build, 'opencv_config', return_value=tmp_path / 'opencv'), \
            patch.object(build.subprocess, 'run', side_effect=StopAfterConfigure) as run, \
            pytest.raises(StopAfterConfigure):
        build.build(args)
    assert opencv.call_args.args[-1] == 'Ninja'
    command = run.call_args.args[0]
    assert command[command.index('-G') + 1] == 'Ninja'
    assert '-DCMAKE_CXX_COMPILER=C:/verified/cl.exe' in command
    assert '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL' in command
    assert '-DCMAKE_MAKE_PROGRAM=/pinned/ninja' in command


def test_workflow_initializes_both_server_toolchains_and_records_outcome():
    root = Path(toolchain.__file__).resolve().parents[2]
    text = (root / '.github/workflows/native-ocr.yml').read_text(encoding='utf-8')
    assert 'os: windows-2022' in text and 'os: windows-2025' in text
    assert text.index('id: windows-toolchain') < text.index('id: native-build')
    assert '--github-env "$env:GITHUB_ENV"' in text
    assert "'--generator Ninja'" in text
    assert '"native-toolchain":"${{ steps.windows-toolchain.outcome }}"' in text
