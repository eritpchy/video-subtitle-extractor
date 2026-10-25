"""Record measured Windows identity and honest, per-stage CPU CI coverage.

Standard-library only: usable before dependency installation and after failures.
Runner labels are expectations, never evidence of the guest OS or process ISA.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys

RUNNERS = {
    'windows-2022': {'product_type': 3, 'build': 20348, 'caption': 'Windows Server 2022'},
    'windows-2025': {'product_type': 3, 'build': 26100, 'caption': 'Windows Server 2025'},
}
MACHINES = {0x014C: 'x86', 0x8664: 'x64', 0xAA64: 'arm64'}
COMMON_STAGES = {
    'identity': 'Measured OS/build and native x64 process',
    'source-tests': 'Source unit/integration regression suite (see JUnit for individual skips)',
    'gui': 'Real Windows Qt GUI launch and screenshots',
    'theme': 'Light/dark/system preference, restart and Qt signal flows (controlled system input)',
    'system-theme': 'Real Server HKCU appearance -> native Windows message -> Qt -> GUI round trip',
    'cli': 'CLI entrypoint',
    'ordinary': 'Ordinary video -> OCR -> SRT text and timing',
    'issues': 'Multiline, AV1 and timeline end-to-end regressions',
    'package-build': 'Self-contained CPU archive build',
    'package-verify': 'Extracted archive GUI, ordinary/multiline/AV1/timeline and runtime/PE isolation',
    'portable-environment': 'Actual launcher full regressions with clean PATH and simulated contaminated host',
}
LIMITS = [
    'Selected Windows scope is Server CI only; client/ARM/GPU machines are not requested or awaited.',
    'Server 2022/2025 results are not Windows 10/11 x64 client results.',
    'Controlled theme tests and the opt-in Server HKCU/native-message round trip are separate; neither is Settings UI automation or Windows 10/11 client coverage.',
    'Windows ARM64/x64-emulation is outside this CI-only scope; native Windows ARM64 packages are not supported.',
    'GPU hardware execution is outside this CI-only scope; CPU fallback and GPU-refusal tests are not GPU passes.',
    'PATH/loaded-DLL/PE isolation on a hosted image is not a clean Windows client VM test.',
    'No claim covers every Windows update, edition, CPU, graphics driver or display scaling.',
]


def architecture(process_machine: int, native_machine: int) -> dict:
    """IsWow64Process2 returns UNKNOWN (0) for a native process, not x86."""
    host = MACHINES.get(native_machine, f'unknown-0x{native_machine:04x}')
    process = MACHINES.get(process_machine or native_machine,
                           f'unknown-0x{process_machine or native_machine:04x}')
    return {'host': host, 'process': process, 'emulated': process != host,
            'process_machine': process_machine, 'native_machine': native_machine,
            'source': 'IsWow64Process2'}


def windows_architecture() -> dict:
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT),
                                      ctypes.POINTER(wintypes.USHORT)]
    kernel.IsWow64Process2.restype = wintypes.BOOL
    process, native = wintypes.USHORT(), wintypes.USHORT()
    if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
        raise ctypes.WinError(ctypes.get_last_error())
    return architecture(process.value, native.value)


def windows_os() -> dict:
    # Use CIM's Caption: the registry's ProductName can still say Windows 10 on 11.
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$os = Get-CimInstance Win32_OperatingSystem
$reg = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
[ordered]@{caption=$os.Caption; version=$os.Version; build=[int]$os.BuildNumber;
    product_type=[int]$os.ProductType; os_architecture=$os.OSArchitecture;
    display_version=$reg.DisplayVersion; edition=$reg.EditionID;
    update_build_revision=$reg.UBR} | ConvertTo-Json -Compress
"""
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', check=True, timeout=60)
    return json.loads(result.stdout.lstrip('\ufeff'))


def validate_identity(identity: dict, runner: str) -> None:
    expected = RUNNERS[runner]
    measured = identity['os']
    if (measured['product_type'] != expected['product_type']
            or measured['build'] != expected['build']
            or expected['caption'] not in measured['caption']):
        raise RuntimeError(f'Runner OS identity mismatch: expected {runner}; measured {measured}')
    arch = identity['architecture']
    if arch['host'] != 'x64' or arch['process'] != 'x64' or arch['emulated']:
        raise RuntimeError(f'Native x64 job must not silently run under emulation: {arch}')
    if identity['python']['pointer_bits'] != 64:
        raise RuntimeError('Windows x64 verification requires a 64-bit interpreter')


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def capture(path: Path, runner: str, profile: str) -> dict:
    identity = {'schema': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
                'runner_label': runner, 'profile': profile, 'validated': False,
                'commit': os.environ.get('GITHUB_SHA'),
                'run_id': os.environ.get('GITHUB_RUN_ID'),
                'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
                'runner_image': os.environ.get('ImageOS'),
                'runner_image_version': os.environ.get('ImageVersion'),
                'runner_arch_environment': os.environ.get('RUNNER_ARCH'),
                'python': {'version': platform.python_version(), 'executable': sys.executable,
                           'pointer_bits': struct.calcsize('P') * 8,
                           'platform_machine': platform.machine()}}
    try:
        if sys.platform != 'win32':
            raise RuntimeError('Windows identity must be measured on Windows, not simulated')
        identity['os'] = windows_os()
        identity['architecture'] = windows_architecture()
        validate_identity(identity, runner)
        identity['validated'] = True
    except Exception as exc:
        identity['error'] = str(exc)
        write_json(path, identity)
        raise
    write_json(path, identity)
    return identity


def system_theme_status(evidence: dict, identity: dict) -> str:
    """A successful GUI step alone is not real system-theme evidence."""
    if not evidence:
        return 'not-run'
    if not isinstance(evidence, dict):
        return 'incomplete'
    if evidence.get('status') != 'passed':
        return evidence.get('status') if evidence.get('status') in ('failed', 'blocked', 'unsupported') else 'incomplete'
    measured = evidence.get('identity', {})
    if (measured.get('runner') != identity.get('runner_label')
            or measured.get('build') != identity.get('os', {}).get('build')
            or measured.get('product_type') != 3
            or any(measured.get(key) != identity.get(key) for key in ('commit', 'run_id', 'run_attempt'))):
        return 'incomplete'
    restoration = evidence.get('restoration', {})
    if (restoration.get('status') != 'restored'
            or evidence.get('original_registry') != restoration.get('registry')
            or not evidence.get('original_registry')
            or (evidence.get('original_registry_key_exists') is False
                and restoration.get('created_key_cleanup') != 'removed-empty-key')):
        return 'incomplete'
    original = evidence.get('original_appearance', {})
    if not original or any(original.get(key) != restoration.get(key) for key in ('qt_scheme', 'effective')):
        return 'incomplete'
    transitions = evidence.get('transitions', [])
    if not isinstance(transitions, list) or len(transitions) != 3:
        return 'incomplete'
    for index, (step, expected) in enumerate(zip(transitions, ('Light', 'Dark', 'Light'))):
        if (step.get('status') != 'passed' or step.get('requested') != expected
                or step.get('qt_scheme') != expected or step.get('effective') != expected
                or step.get('preference') != 'Auto'
                or step.get('registry') != {'exists': True, 'type': 4, 'value': int(expected == 'Light')}
                or (index and expected not in step.get('qt_signals', []))):
            return 'incomplete'
    for page in ('home', 'timeline', 'settings'):
        values = [step.get('page_lightness', {}).get(page) for step in transitions]
        if (not all(isinstance(value, (int, float)) for value in values)
                or values[0] <= values[1] + 80 or abs(values[0] - values[2]) > 3):
            return 'incomplete'
    return 'passed'


def coverage(identity: dict, outcomes: dict, profile: str, system_theme=None) -> dict:
    stages = dict(COMMON_STAGES)
    if profile == 'native':
        stages = {'native-toolchain': 'Verify installed x64 MSVC/CRT source compatibility and pinned Ninja',
                  'native-build': 'Compile native CPU worker and stage pinned libraries/models',
                  'native-smoke': 'Worker protocol, model parity and explicit GPU refusal', **stages}
    records = []
    for key, scope in stages.items():
        outcome = outcomes.get(key, '')
        status = {'success': 'passed', 'failure': 'failed', 'cancelled': 'cancelled',
                  'skipped': 'not-run', '': 'not-run'}.get(outcome, 'unknown')
        if key == 'identity' and not identity.get('validated'):
            status = 'failed' if outcome in ('success', 'failure') else 'not-run'
        if key == 'system-theme':
            status = system_theme_status(system_theme, identity)
        records.append({'stage': key, 'scope': scope, 'outcome': outcome, 'status': status})
    passed = (all(record['status'] == 'passed' for record in records)
              and outcomes.get('license-gate') not in ('failure', 'cancelled'))
    return {'schema': 1, 'profile': profile, 'execution_status': 'passed' if passed else 'incomplete-or-failed',
            'identity': identity, 'stages': records, 'limitations': LIMITS,
            'system_theme_evidence': system_theme or {'status': 'not-run'},
            'customer_package_license_gate': outcomes.get('license-gate', 'not-applicable'),
            'coverage_policy': 'server-ci-only-no-client-machine-access',
            'gpu_execution': 'outside-ci-only-scope', 'client_windows_execution': 'outside-ci-only-scope',
            'windows_arm_execution': 'outside-ci-only-scope',
            'scope': 'This exact runner image, commit and CPU profile only; not a support certification'}


def dependency_versions() -> dict:
    versions = {}
    for name in ('PySide6', 'PySide6-Essentials', 'shiboken6', 'numpy', 'paddlepaddle',
                 'paddleocr', 'paddlex', 'opencv-contrib-python', 'opencv-contrib-python-headless'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def markdown(report: dict) -> str:
    identity = report['identity']
    measured = identity.get('os', {})
    arch = identity.get('architecture', {})
    lines = [f"## Windows verification / {report['profile']} CPU / {identity.get('runner_label', 'unknown')}",
             '', f"Result: **{report['execution_status']}** (not a client Windows certification).",
             f"Commit: `{identity.get('commit', 'not captured')}`; runner image: "
             f"{identity.get('runner_image', '?')} / {identity.get('runner_image_version', '?')}; "
             f"source Python: {identity.get('python', {}).get('version', '?')}.",
             f"Measured OS: {measured.get('caption', 'not captured')}; build "
             f"{measured.get('build', '?')}.{measured.get('update_build_revision', '?')}; "
             f"host={arch.get('host', '?')}, process={arch.get('process', '?')}, "
             f"emulated={arch.get('emulated', '?')}.", '', '| Check | Status |', '| --- | --- |']
    lines.extend(f"| {row['scope']} | {row['status']} |" for row in report['stages'])
    if identity.get('error'):
        lines += ['', 'Identity error: ' + identity['error']]
    theme = report.get('system_theme_evidence', {})
    if theme.get('error'):
        lines += ['', 'Real system-theme evidence: ' + theme['error']]
    if report['profile'] == 'native':
        lines += ['', 'Customer-package VC license gate outcome: '
                  + report['customer_package_license_gate'] + ' (runner execution does not grant redistribution rights).']
    lines += ['', 'Coverage boundaries:', *('- ' + limit for limit in LIMITS), '']
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('capture', 'summarize'))
    parser.add_argument('--runner', required=True, choices=tuple(RUNNERS))
    parser.add_argument('--profile', required=True, choices=('paddle', 'native'))
    parser.add_argument('--output', type=Path, default=Path('test-results/windows'))
    parser.add_argument('--system-theme-report', type=Path)
    args = parser.parse_args(argv)
    identity_path = args.output / 'identity.json'
    if args.command == 'capture':
        capture(identity_path, args.runner, args.profile)
    else:
        identity = json.loads(identity_path.read_text(encoding='utf-8')) if identity_path.exists() else {
            'runner_label': args.runner, 'profile': args.profile, 'validated': False,
            'error': 'Identity capture did not run or produce evidence'}
        theme = None
        if args.system_theme_report and args.system_theme_report.exists():
            try:
                theme = json.loads(args.system_theme_report.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                theme = {'status': 'failed', 'error': f'Could not read real system-theme evidence: {exc}'}
        report = coverage(identity, json.loads(os.environ.get('VSE_WINDOWS_STEP_OUTCOMES', '{}')), args.profile, theme)
        report['source_dependency_versions'] = dependency_versions()
        write_json(args.output / 'coverage.json', report)
        summary = markdown(report)
        (args.output / 'coverage.md').write_text(summary, encoding='utf-8')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as stream:
                stream.write(summary)
        print(summary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
