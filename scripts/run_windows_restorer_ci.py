#!/usr/bin/env python3
"""Independent native Server diagnostic; no customer distribution or publishing."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_toolchain():
    spec = importlib.util.spec_from_file_location('restorer_ci_toolchain', ROOT / 'native/ocr/windows_toolchain.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def select_installed(tools, instances, runtime, report):
    eligible = []
    report['candidates'] = []
    for instance in instances:
        installation = Path(instance['installationPath'])
        for directory in sorted((installation / 'VC/Tools/MSVC').glob('*')):
            if not directory.is_dir():
                continue
            row = {'installation': str(installation), 'visual_studio_version': instance['installationVersion'],
                   'toolset': directory.name}
            try:
                row.update(tools.inspect_toolset(directory))
                tools.check_versions(row['toolset'], row['compiler']['version'], row['linker']['version'],
                                     row['crt']['version'], runtime)
                if not (installation / 'Common7/Tools/VsDevCmd.bat').is_file():
                    raise RuntimeError('VsDevCmd.bat missing')
                row['compatible'] = True
                eligible.append(row)
            except (OSError, ValueError, RuntimeError) as error:
                row.update(compatible=False, reason=str(error))
            report['candidates'].append(row)
    if not eligible:
        raise RuntimeError('No reviewed installed MSVC 14.44 toolset; no installer or fallback was attempted')
    selected = max(eligible, key=lambda row: tools.version(row['toolset']))
    report['selected'] = selected
    environment = tools.initialize_environment(selected, report)
    active = tools.validate_environment(environment, runtime)
    tools.bind_selected_toolset(active, selected)
    # Shared validation checks provenance; this candidate actually compiles /MT,
    # never distributes the Native worker's /MD runtime or inherits its result.
    active['runtime_linkage'] = 'MultiThreaded (/MT); actual PE imports are a separate mandatory gate'
    active['loader_compatibility'] = 'Pending independent synthetic Server restorer execution'
    report['active'] = active
    return environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-directory', required=True, type=Path)
    parser.add_argument('--report-directory', required=True, type=Path)
    parser.add_argument('--runner', required=True, choices=['windows-2022', 'windows-2025'])
    args = parser.parse_args(argv)
    args.report_directory.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'scope': 'synthetic-restorer-diagnostic-not-package-acceptance',
              'result': 'failed', 'runner_label': args.runner, 'actual_os': platform.platform(),
              'commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
              'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'image_os': os.environ.get('ImageOS'),
              'image_version': os.environ.get('ImageVersion'), 'gpu_inference_verified': False,
              'client_windows_10_11_verified': False, 'release_distribution_created': False}
    try:
        if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
            raise RuntimeError('Authorized native Windows x64 Server runners only')
        actual = sys.getwindowsversion()
        expected = {'windows-2022': 20348, 'windows-2025': 26100}[args.runner]
        report['os_build'] = actual.build
        report['os_product_type'] = actual.product_type
        if actual.build != expected or actual.product_type != 3:
            raise RuntimeError('Actual Windows Server build/product type differs from runner label')
        tools = load_toolchain()
        runtime = json.loads((ROOT / 'native/ocr/vc-runtime-manifest.json').read_text(encoding='utf-8'))['version']
        vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        report['vswhere'] = {'path': str(vswhere), 'sha256': hashlib.sha256(vswhere.read_bytes()).hexdigest()}
        instances = json.loads(subprocess.check_output([
            str(vswhere), '-all', '-products', '*', '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json', '-utf8',
        ], encoding='utf-8', timeout=60))
        environment = select_installed(tools, instances, runtime, report)
        args.work_directory.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'native/windows-restorer/tests/windows_server_tests.py'),
                   '--work-directory', str(args.work_directory.absolute()), '--large']
        report['command'] = command
        env = {key.upper(): value for key, value in os.environ.items()}
        env.update(environment)
        env['VSLANG'] = '1033'  # Stable build-side compiler diagnostics, not customer UI.
        with (args.report_directory / 'native-tests.log').open('wb') as output:
            result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT,
                                    timeout=65 * 60, check=False)
        report['returncode'] = result.returncode
        result.check_returncode()
        child = json.loads((args.work_directory / 'native-restorer-report.json').read_text(encoding='utf-8'))
        if (child.get('result') != 'passed' or child.get('large_requested') is not True or
                child.get('scope') != 'synthetic-native-restorer-tests-not-package-acceptance'):
            raise RuntimeError('Complete large native diagnostic receipt missing')
        report['result'] = 'passed'
    except Exception as error:
        report['reason'] = str(error)
        raise
    finally:
        (args.report_directory / 'toolchain-and-run.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
