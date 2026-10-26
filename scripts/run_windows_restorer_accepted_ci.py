#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Dedicated accepted-306 experiment; shared installed-toolchain policy, no publish."""
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def diagnostic_command(work, pins, profile, source, runner):
    return [sys.executable, '-B', '-X', 'utf8', str(ROOT / 'native/windows-restorer-accepted/accepted_server_tests.py'),
            '--work-directory', str(work.absolute()), '--pins', str(pins.absolute()), '--profile', profile,
            '--baseline-source', str(source.absolute()), '--runner', runner]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-directory', required=True, type=Path)
    parser.add_argument('--report-directory', required=True, type=Path)
    parser.add_argument('--runner', required=True, choices=['windows-2022', 'windows-2025'])
    parser.add_argument('--accepted-baseline-pins', required=True, type=Path)
    parser.add_argument('--accepted-profile', required=True, choices=['paddle-cuda11.8', 'paddle-cuda12.6'])
    parser.add_argument('--baseline-source', required=True, type=Path)
    args = parser.parse_args(argv)
    args.report_directory.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'scope': 'accepted-306-baseline-native-restore-not-final-release', 'result': 'failed',
              'runner_label': args.runner, 'actual_os': platform.platform(), 'accepted_profile': args.accepted_profile,
              'commit': os.environ.get('GITHUB_SHA'), 'run_id': os.environ.get('GITHUB_RUN_ID'),
              'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'), 'image_os': os.environ.get('ImageOS'),
              'image_version': os.environ.get('ImageVersion'), 'gpu_inference_verified': False,
              'client_windows_10_11_verified': False, 'release_distribution_created': False}
    try:
        if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
            raise RuntimeError('Authorized native Windows x64 Server runners only')
        actual = sys.getwindowsversion()
        report.update(os_build=actual.build, os_product_type=actual.product_type)
        if actual.build != {'windows-2022': 20348, 'windows-2025': 26100}[args.runner] or actual.product_type != 3:
            raise RuntimeError('Actual Windows Server build/product type differs from runner label')
        shared = load('unchanged_restorer_ci', ROOT / 'scripts/run_windows_restorer_ci.py')
        tools = shared.load_toolchain()
        runtime = json.loads((ROOT / 'native/ocr/vc-runtime-manifest.json').read_text(encoding='utf-8'))['version']
        vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
        report['vswhere'] = {'path': str(vswhere), 'sha256': hashlib.sha256(vswhere.read_bytes()).hexdigest()}
        instances = json.loads(subprocess.check_output([str(vswhere), '-all', '-products', '*', '-requires',
            'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json', '-utf8'], encoding='utf-8', timeout=60))
        environment = shared.select_installed(tools, instances, runtime, report)
        report['active']['loader_compatibility'] = 'Pending independent actual accepted-306 Server restoration'
        command = diagnostic_command(args.work_directory, args.accepted_baseline_pins, args.accepted_profile, args.baseline_source, args.runner)
        report['command'] = command
        env = {key.upper(): value for key, value in os.environ.items()}; env.update(environment); env['VSLANG'] = '1033'
        with (args.report_directory / 'accepted-native-tests.log').open('wb') as output:
            result = subprocess.run(command, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=220 * 60, check=False)
        report['returncode'] = result.returncode; result.check_returncode()
        policy = load('restorer_accepted_policy', ROOT / 'native/windows-restorer-accepted/accepted_baseline.py')
        child = json.loads((args.work_directory / 'accepted-restorer-report.json').read_text(encoding='utf-8'))
        policy.validate_run_report(child, policy.load_pins(args.accepted_baseline_pins), args.accepted_profile, args.runner)
        report['result'] = 'passed'
    except Exception as error:
        report['reason'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        (args.report_directory / 'toolchain-and-run.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__': raise SystemExit(main())
