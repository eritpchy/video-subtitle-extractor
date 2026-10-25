"""Build-command policy only; actual macOS parity remains the native smoke gate."""
from unittest.mock import patch

import build_opencv


def configured_flags(tmp_path, system):
    with patch.object(build_opencv.platform, 'system', return_value=system), \
            patch.object(build_opencv.subprocess, 'run') as run:
        build_opencv.build(tmp_path / 'source', tmp_path / 'build', tmp_path / 'install')
    assert run.call_count == 3
    return dict(argument[2:].split('=', 1) for argument in run.call_args_list[0].args[0]
                if argument.startswith('-D'))


def test_mac_opencv_disables_contraction_inside_the_library(tmp_path):
    flags = configured_flags(tmp_path, 'Darwin')
    for language in ('C', 'CXX'):
        options = flags[f'CMAKE_{language}_FLAGS_RELEASE'].split()
        assert options[-2:] == ['-fno-fast-math', '-ffp-contract=off']
        assert '-ffast-math' not in options
    assert flags['CMAKE_EXPORT_COMPILE_COMMANDS'] == 'ON'
    assert flags['CMAKE_OSX_ARCHITECTURES'] == 'arm64'
    assert flags['CMAKE_OSX_DEPLOYMENT_TARGET'] == '14.0'
    # This fixes source arithmetic; do not disable SIMD/HAL or alter models.
    assert flags['CPU_DISPATCH'] == ''
    assert 'WITH_CAROTENE' not in flags


def test_linux_opencv_baseline_policy_is_unchanged(tmp_path):
    flags = configured_flags(tmp_path, 'Linux')
    assert flags['CPU_BASELINE'] == 'SSE3'
    for language in ('C', 'CXX'):
        assert flags[f'CMAKE_{language}_FLAGS_RELEASE'] == '-O3 -DNDEBUG -ffunction-sections -fdata-sections'
    assert 'CMAKE_OSX_ARCHITECTURES' not in flags
