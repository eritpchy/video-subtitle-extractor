#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Post-acceptance, read-only OS capture controls; never a replacement acceptance.

No probe is run until the unchanged original two-scenario attempt has finished
and its evidence has been hashed. A derived same-configuration environment is
NOT a reconstruction of the old failure's complete instantaneous environment.
"""
from __future__ import annotations
import argparse
import ast
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BASELINE = '3064320ec74e1aa71f3702c2ea966485f4b31e33'
SOURCE_SHA = '750f7a0543b687215889c3b69b5a401734716df5a2c3316dea8b509639a38fc3'
# Exact whole-source pins, including Git's Windows checkout newline variant.
SOURCE_CRLF_SHA = '4cb59b32f534a882f6f96cbdfeab9439d90b7a36cbaffb8fd39601d6013290ba'
RUNNER = 'windows-2025'
PROFILE = 'paddle-cuda12.6'
SCENARIOS = ('clean-host-path', 'contaminated-host-environment')
TIMEOUT = 60
STAGES = ('enter', 'before_cim', 'after_cim', 'before_registry', 'after_registry', 'before_json', 'after_json')
INHERITED = frozenset(('SYSTEMROOT WINDIR COMSPEC TEMP TMP NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE '
    'PROGRAMFILES PROGRAMFILES(X86) PROGRAMDATA SYSTEMDRIVE USERPROFILE USERNAME USERDOMAIN OS').split())
# Only these selected non-sensitive values are persisted. Other env values are
# never serialized, even if a caller adds new fields later.
LOG_VALUES = frozenset(('SYSTEMROOT WINDIR PATH TEMP TMP HOME USERPROFILE APPDATA LOCALAPPDATA '
    'PSMODULEPATH PYTHONNOUSERSITE PYTHONUTF8 PYTHONDONTWRITEBYTECODE VSE_WINDOWS_RUNNER_LABEL').split())
OVERLAY = frozenset(('PATH PYTHONPATH PYTHONNOUSERSITE PYTHONUTF8 VSE_CONFIG_FILE VSE_WORK_DIR '
    'VSE_OCR_BACKEND VSE_MODEL_DIR VSE_VSF_BINARY VSE_VSF_DIR IMAGEIO_FFMPEG_EXE').split())
FORBIDDEN_PREFIXES = ('VSE_RUNTIME_AUDIT_',)


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, document):
    with Path(path).open('x', encoding='utf-8', newline='\n') as output:
        json.dump(document, output, indent=2, ensure_ascii=False); output.write('\n')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def snapshot(paths):
    result = {}
    for base in paths:
        if not base.exists(): raise ValueError('Required completed-attempt evidence is missing')
        entries = [base, *base.rglob('*')] if base.is_dir() else [base]
        for path in entries:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Evidence contains a link/reparse point')
            if stat.S_ISDIR(info.st_mode): continue
            if not stat.S_ISREG(info.st_mode): raise ValueError('Evidence is not a regular file')
            result[str(path)] = {'bytes': info.st_size, 'sha256': sha(path)}
    return result


def original_command(path):
    """Read the uniquely pinned source; never execute/eval extracted AST."""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() not in (SOURCE_SHA, SOURCE_CRLF_SHA):
        raise ValueError('OS capture source is not exact accepted 306 bytes')
    tree = ast.parse(raw.decode('utf-8'))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'windows_os']
    if len(functions) != 1: raise ValueError('Ambiguous windows_os function')
    commands = [n.value for n in ast.walk(functions[0]) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'command' for t in n.targets)]
    calls = [n for n in ast.walk(functions[0]) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id == 'subprocess' and n.func.attr == 'run']
    if len(commands) != 1 or not isinstance(commands[0], ast.Constant) or not isinstance(commands[0].value, str) or len(calls) != 1:
        raise ValueError('Expected unique literal PowerShell capture')
    timeout = [k.value for k in calls[0].keywords if k.arg == 'timeout']
    if len(timeout) != 1 or not isinstance(timeout[0], ast.Constant) or type(timeout[0].value) is not int or timeout[0].value != TIMEOUT:
        raise ValueError('Original timeout changed')
    return commands[0].value


def marked_command(command):
    # Original operations/stdout are unchanged. Marker overhead itself is a
    # diagnostic difference, not a claim to reproduce the prior failure.
    prefix = """function VseOsStage([string]$name) {
 [Console]::Error.WriteLine(('VSE_OS_STAGE|{0}|{1}|{2}|{3}|{4}' -f $name, $PID, [System.Diagnostics.Stopwatch]::GetTimestamp(), [System.Diagnostics.Stopwatch]::Frequency, [DateTime]::UtcNow.ToString('o')))
 [Console]::Error.Flush()
}
VseOsStage 'enter'
"""
    replacements = [
        ('$os = Get-CimInstance Win32_OperatingSystem', "VseOsStage 'before_cim'\n$os = Get-CimInstance Win32_OperatingSystem\nVseOsStage 'after_cim'"),
        ("$reg = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'", "VseOsStage 'before_registry'\n$reg = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'\nVseOsStage 'after_registry'"),
        ('[ordered]@{caption=', "VseOsStage 'before_json'\n[ordered]@{caption="),
        ('| ConvertTo-Json -Compress', "| ConvertTo-Json -Compress\nVseOsStage 'after_json'"),
    ]
    for old, new in replacements:
        if command.count(old) != 1: raise ValueError('Original stage anchors changed')
        command = command.replace(old, new)
    return prefix + command


def stages_from(stderr, pid):
    stages = []
    for line in stderr.decode('utf-8', errors='replace').splitlines():
        if not line.startswith('VSE_OS_STAGE'): continue
        fields = line.split('|')
        if (len(fields) != 6 or fields[0] != 'VSE_OS_STAGE' or fields[1] not in STAGES
                or not all(v.isdigit() for v in fields[2:5]) or int(fields[2]) != pid
                or int(fields[4]) <= 0):
            raise ValueError('Malformed diagnostic stage marker')
        at = len(stages)
        if at >= len(STAGES) or fields[1] != STAGES[at]: raise ValueError('Missing/duplicate/out-of-order stage')
        utc = datetime.fromisoformat(fields[5].replace('Z', '+00:00'))
        if utc.utcoffset() is None or utc.utcoffset().total_seconds() != 0: raise ValueError('Stage UTC missing')
        row = {'stage': fields[1], 'pid': int(fields[2]), 'stopwatch_ticks': int(fields[3]), 'stopwatch_frequency': int(fields[4]), 'utc': fields[5]}
        if stages and (row['stopwatch_ticks'] < stages[-1]['stopwatch_ticks'] or row['stopwatch_frequency'] != stages[0]['stopwatch_frequency']):
            raise ValueError('Non-monotonic stage timing')
        stages.append(row)
    return stages


def safe_environment_record(environment, inherited):
    upper = {key.upper(): value for key, value in environment.items()}
    # No full environment serialization; arbitrary/token-like names are counted,
    # not copied. Only explicit OS allowlist names and selected values appear.
    return {'scope': 'new-attempt-derived-configuration-not-old-instantaneous-environment',
        'selected_non_sensitive_values': {k: upper[k] for k in sorted(LOG_VALUES) if k in upper},
        'selected_absent_names': sorted(LOG_VALUES - upper.keys()),
        'inherited_allowlist': [{'name': k, 'present': any(n.upper() == k for n in inherited), 'filtered': False} for k in sorted(INHERITED)],
        'other_inherited_names_and_values_not_recorded_count': sum(k.upper() not in INHERITED for k in inherited),
        'audit_hooks_removed': ['PYTHONPATH', 'VSE_RUNTIME_AUDIT_*'],
        'old_full_environment_available': False}


def without_hooks(environment):
    return {key: value for key, value in environment.items()
            if key.upper() != 'PYTHONPATH' and not key.upper().startswith(FORBIDDEN_PREFIXES)}


def system_powershell(environment):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer): raise ctypes.WinError(ctypes.get_last_error())
    system = Path(buffer.value) / 'WindowsPowerShell/v1.0/powershell.exe'
    upper = {k.upper(): v for k, v in environment.items()}
    original = Path(upper['SYSTEMROOT']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not original.is_absolute() or not original.samefile(system) or sha(original) != sha(system):
        raise ValueError('Original SystemRoot PowerShell is not the native system executable')
    return original, {'original_path': str(original), 'GetSystemDirectoryW_path': str(system),
                      'same_file': True, 'sha256': sha(original), 'bytes': original.stat().st_size}


def run_capture(executable, command, root, environment, directory, marked, validator):
    directory.mkdir()
    record = {'result': 'failed', 'timeout_seconds': TIMEOUT, 'marked': marked,
              'cwd': str(root), 'argv': [str(executable), '-NoProfile', '-NonInteractive', '-Command', command],
              'command_sha256': hashlib.sha256(command.encode('utf-8')).hexdigest(),
              'utc_start': datetime.now(timezone.utc).isoformat(), 'monotonic_start_ns': time.monotonic_ns(),
              'timeout_scope': 'communicate budget; process creation may add wall time'}
    out = err = b''
    try:
        with subprocess.Popen(record['argv'], cwd=root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            record['pid'] = process.pid
            try: out, err = process.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                record['timed_out'] = True
                process.kill()
                # Retry communicate retains the complete partial streams; do not
                # concatenate TimeoutExpired.output (would duplicate its prefix).
                out, err = process.communicate()
            else: record['timed_out'] = False
            record['returncode'] = process.returncode
        if marked: record['stages'] = stages_from(err, record['pid'])
        if record['timed_out']: raise RuntimeError('Original 60-second PowerShell budget exceeded')
        if record['returncode'] != 0: raise RuntimeError('PowerShell returned nonzero')
        if len(out) > 2 * 1024**2 or len(err) > 2 * 1024**2: raise RuntimeError('Unexpected oversized OS output')
        if marked and [r['stage'] for r in record['stages']] != list(STAGES): raise RuntimeError('Incomplete successful stage trace')
        measured = json.loads(out.decode('utf-8-sig'))
        validator(measured)
        record['os'] = measured; record['result'] = 'passed'
    except Exception as error:
        record['error'] = f'{type(error).__name__}: {error}'
    finally:
        record['utc_end'] = datetime.now(timezone.utc).isoformat(); record['monotonic_end_ns'] = time.monotonic_ns()
        (directory / 'stdout.bin').write_bytes(out); (directory / 'stderr.bin').write_bytes(err)
        # Binary bytes are authoritative; text copies are for UI viewing only.
        for name, raw in (('stdout', out), ('stderr', err)):
            (directory / (name + '.log')).write_text(raw.decode('utf-8', errors='replace'), encoding='utf-8')
            record[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        write_json(directory / 'capture.json', record)
    return record


def registry_control():
    import winreg
    path = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    record = {'hive': 'HKLM', 'key': path, 'view': '64-bit', 'read_only': True, 'values': {}}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
        for name in ('DisplayVersion', 'EditionID', 'UBR', 'CurrentBuildNumber'):
            try:
                value, kind = winreg.QueryValueEx(key, name)
                if kind not in (winreg.REG_SZ, winreg.REG_DWORD) or not isinstance(value, (str, int)):
                    raise ValueError('Unexpected OS registry value type')
                record['values'][name] = {'present': True, 'type': kind, 'value': value}
            except FileNotFoundError:
                record['values'][name] = {'present': False}
    return record


def native_control(module):
    from ctypes import wintypes
    class Version(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('major', wintypes.DWORD), ('minor', wintypes.DWORD),
                    ('build', wintypes.DWORD), ('platform', wintypes.DWORD), ('service_pack', wintypes.WCHAR * 128),
                    ('service_major', wintypes.WORD), ('service_minor', wintypes.WORD), ('suite', wintypes.WORD),
                    ('product_type', wintypes.BYTE), ('reserved', wintypes.BYTE)]
    if ctypes.sizeof(Version) != 284: raise ValueError('Unexpected OSVERSIONINFOEXW ABI')
    version = Version(); version.size = ctypes.sizeof(version)
    api = ctypes.WinDLL('ntdll').RtlGetVersion
    api.argtypes = [ctypes.POINTER(Version)]; api.restype = wintypes.LONG
    status = api(ctypes.byref(version))
    if status != 0: raise OSError(f'RtlGetVersion NTSTATUS {status}')
    value = sys.getwindowsversion()
    record = {'scope': 'read-only numeric comparison, never CIM acceptance replacement',
              'RtlGetVersion': {'major': version.major, 'minor': version.minor, 'build': version.build, 'product_type': version.product_type, 'ntstatus': status},
              'sys_getwindowsversion': {'major': value.major, 'minor': value.minor, 'build': value.build, 'product_type': value.product_type},
              'architecture': module.windows_architecture(), 'registry': registry_control()}
    if record['RtlGetVersion']['build'] != 26100 or value.build != 26100 or version.product_type != 3 or value.product_type != 3:
        raise ValueError('Native OS controls differ from authorized Server 2025')
    return record


def child(root, output):
    """Run with the verified bundle's Python, -I -B, no old audit-hook writes."""
    if output.resolve().is_relative_to(root.resolve()):
        raise ValueError('Diagnostic output must be outside the restored payload')
    output.mkdir()
    report = {'result': 'failed', 'python': sys.executable, 'isolated': sys.flags.isolated,
              'bytecode_disabled': sys.dont_write_bytecode, 'probes': []}
    try:
        if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
            raise RuntimeError('Isolated native Windows bundled interpreter required')
        source = root / 'scripts/windows_verification.py'; command = original_command(source)
        module = load('accepted_os_verification', source)
        executable, report['powershell'] = system_powershell(os.environ)
        report['source_sha256'] = sha(source)
        report['source_newline_form'] = 'CRLF' if report['source_sha256'] == SOURCE_CRLF_SHA else 'LF'
        # Control precedes instrumented query. Native API comparison is after
        # both, so it is never an added prewarm before the unmarked control.
        def validate(measured):
            module.validate_identity({'os': measured, 'architecture': module.windows_architecture(),
                                      'python': {'pointer_bits': struct.calcsize('P') * 8}}, RUNNER)
        for label, script, marked in (('original-control', command, False), ('stage-marked', marked_command(command), True)):
            report['probes'].append(run_capture(executable, script, root, dict(os.environ), output / label, marked, validate))
        report['native_control'] = native_control(module)
        if sha(executable) != report['powershell']['sha256']: raise RuntimeError('PowerShell executable changed')
        if any(row['result'] != 'passed' for row in report['probes']): raise RuntimeError('At least one independent OS control failed')
        report['result'] = 'passed'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally: write_json(output / 'child-report.json', report)
    return 0 if report['result'] == 'passed' else 1


def derive_environment(portable, bootstrap, root, work, output, scenario, inherited):
    allowed = {k.upper(): v for k, v in inherited.items() if k.upper() in INHERITED}
    allowed.update(VSE_WINDOWS_RUNNER_LABEL=RUNNER, PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
    template = output / ('template-' + scenario)
    environment, fixture = portable.fixture_environment(template, scenario == SCENARIOS[1], allowed)
    old = work / 'launcher-evidence' / ('fixture-' + scenario)
    def remap(value):
        if isinstance(value, str): return value.replace(str(template), str(old))
        if isinstance(value, list): return [remap(item) for item in value]
        if isinstance(value, dict): return {k: remap(v) for k, v in value.items()}
        return value
    case = work / 'launcher-evidence' / scenario
    recorded_fixture = json.loads((case / 'fixture.json').read_bytes())
    if remap(fixture) != recorded_fixture: raise ValueError('Derived baseline fixture differs from completed attempt')
    environment = bootstrap.sanitize_environment(root, remap(environment), 'windows')
    observed = json.loads((case / 'environment-report.json').read_bytes())
    if observed.get('bundle_root') != str(root) or observed.get('platform') != 'windows':
        raise ValueError('Observed environment does not bind restored root')
    selected = observed['environment']
    if not isinstance(selected, dict) or set(selected) - OVERLAY:
        raise ValueError('Unexpected captured environment keys; refusing arbitrary/secret overlays')
    if selected.get('PATH') != environment['PATH'] or selected.get('VSE_CONFIG_FILE') != environment['VSE_CONFIG_FILE'] or selected.get('VSE_WORK_DIR') != environment['VSE_WORK_DIR']:
        raise ValueError('New attempt PATH/config/work differ from derived baseline configuration')
    environment.update(selected)
    environment['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    environment = without_hooks(environment)
    record = safe_environment_record(environment, inherited)
    record.update(captured_environment_report_sha256=sha(case / 'environment-report.json'),
                  fixture_sha256=sha(case / 'fixture.json'), observed_overlay_names=sorted(selected),
                  values_not_in_environment_report_source='same-job filtered host plus exact baseline fixture constructor and sanitizer',
                  intentional_difference='No Python/runtime audit hooks; bundled diagnostic starts -I -B; PS cwd remains restored root')
    return environment, record


def require_completed_attempt(report, launcher):
    if (report.get('profile') != PROFILE or report.get('runner') != RUNNER or report.get('baseline_commit') != BASELINE
            or report.get('restore_returncode') != 0 or report.get('original_acceptance_validation') != 'passed-release-guard-bound-to-original-artifact-and-ZIP'
            or report.get('originals_unchanged_verified') is not True or report.get('volumes_unchanged_verified') is not True
            or report.get('post_run_errors') != [] or report.get('restored_before_launcher') != report.get('restored_after_launcher')
            or report.get('restored_after_launcher', {}).get('result') != 'passed'):
        raise ValueError('A completed integrity-verified native restoration is required before post probes')
    cases = launcher.get('scenarios', [])
    if [case.get('scenario') for case in cases] != list(SCENARIOS) or any(case.get('result') not in ('passed', 'failed') for case in cases):
        raise ValueError('Both original launcher scenarios must terminate before post probes')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--work-directory', type=Path)
    parser.add_argument('--report-directory', type=Path)
    parser.add_argument('--baseline-source', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.child:
        if args.root is None: parser.error('--child needs --root')
        return child(args.root.resolve(strict=True), args.output.absolute())
    if not all((args.work_directory, args.report_directory, args.baseline_source)):
        parser.error('Post-only orchestrator needs completed work/report and baseline source')
    output = args.output.resolve()
    for protected in (args.work_directory, args.report_directory, args.baseline_source):
        if output.is_relative_to(protected.resolve()) or protected.resolve().is_relative_to(output):
            raise ValueError('Diagnostic output must be separate from original work/reports/source')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 1, 'result': 'failed', 'scope': 'single-server-2025-cuda12.6-post-acceptance-diagnostic-only',
        'prior_run': {'id': 40308041109, 'result': '3-passed-1-failed', 'failed_job': 121521944053,
                      'failure_scope': 'whole verification-only PowerShell child timeout; stage/root cause unknown',
                      'first_failure_raw_sha256': 'b7873a85703b6d9cc786dcba229c0e37623578cfce5fb1dbfe8a9b625407d532',
                      'four_job_raw_library_id': 'libfile_6002b22312008191b4389dc84f7d2a43',
                      'four_job_raw_bytes': 14555022,
                      'four_job_raw_sha256': '457321932e547d0a599f5b0787497a418c46f4aaeff9c92aa1a521d44c29f3c8'},
        'prior_failure_reclassified': False, 'new_run_id': os.environ.get('GITHUB_RUN_ID'),
        'new_commit': os.environ.get('GITHUB_SHA'), 'no_added_pre_acceptance_probe': True,
        'cold_WMI_state_claimed': False, 'old_full_environment_reconstructed': False,
        'gpu_inference_verified': False, 'scenarios': [], 'after_errors': []}
    frozen = None; original = None; policy = None; expected = None; volume = None
    try:
        if os.name != 'nt': raise RuntimeError('Actual native Windows Server only')
        work = args.work_directory.resolve(strict=True)
        source = args.baseline_source.resolve(strict=True)
        if subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() != BASELINE or subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip():
            raise ValueError('Baseline audit checkout differs from exact clean 306')
        original = json.loads((work / 'accepted-restorer-report.json').read_bytes())
        launcher = json.loads((work / 'launcher-evidence/restored-launcher-report.json').read_bytes())
        require_completed_attempt(original, launcher)
        report['new_original_acceptance_result'] = original['result']
        report['new_original_scenario_results'] = [{k: row[k] for k in ('scenario', 'result')} for row in launcher['scenarios']]
        frozen_paths = [work / 'accepted-restorer-report.json', work / 'launcher-evidence', args.report_directory.resolve(strict=True)]
        frozen = snapshot(frozen_paths)
        write_json(output / 'frozen-original-evidence.json', frozen)
        (output / 'original-accepted-restorer-report.json').write_bytes((work / 'accepted-restorer-report.json').read_bytes())
        # Original attempt is now frozen. Only now import helpers/check archive
        # and perform the separately reported post-only environment controls.
        sys.path.insert(0, str(REPO))
        from scripts import release_volume_transport as transport
        policy = load('os_diagnostic_accepted_policy', REPO / 'native/windows-restorer-accepted/accepted_baseline.py')
        pins = policy.load_pins(REPO / 'native/windows-restorer-accepted/accepted-baseline-306.json')
        row = next(r for r in pins['profiles'] if r['profile'] == PROFILE)
        if original['archive_sha256'] != row['archive_sha256']: raise ValueError('Wrong original accepted ZIP')
        manifests = list((work / 'volumes').glob('*.transport.json'))
        if len(manifests) != 1: raise ValueError('Expected unique original transport manifest')
        volume = manifests[0]
        document = transport.verify_transport(volume, include_entries=True)
        if document['archive']['sha256'] != row['archive_sha256'] or document['archive']['identity']['commit'] != BASELINE:
            raise ValueError('Transport differs from the fixed accepted original')
        expected = policy.inventory(document)
        destination = work / 'restored Unicode 中文 &!()'
        root = destination / document['archive']['root']
        report['before_probe_tree'] = policy.verify_restored_tree(destination, expected)
        if report['before_probe_tree'] != original['restored_after_launcher']: raise ValueError('Completed restoration changed before probes')
        if original_command(root / 'scripts/windows_verification.py') != original_command(source / 'scripts/windows_verification.py'):
            raise ValueError('Restored/source OS command mismatch')
        # Avoid mixing current scripts module cache with 306 helper imports.
        for name in list(sys.modules):
            if name == 'scripts' or name.startswith('scripts.'): del sys.modules[name]
        sys.path.insert(0, str(source))
        from scripts import verify_portable_environment as portable
        from scripts import bundle_bootstrap as bootstrap
        companion = output / 'diagnostic-source'; companion.mkdir()
        report['diagnostic_source_sha256'] = {}
        for path in [*HERE.rglob('*.py'), HERE / 'README.md', REPO / '.github/workflows/windows-restorer-os-diagnostic.yml']:
            name = path.relative_to(REPO).as_posix(); target = companion / name
            target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(path.read_bytes())
            report['diagnostic_source_sha256'][name] = sha(path)
        (output / 'workflow-source.yml').write_bytes((REPO / '.github/workflows/windows-restorer-os-diagnostic.yml').read_bytes())
        for scenario in SCENARIOS:
            case = {'scenario': scenario, 'result': 'failed'}; report['scenarios'].append(case)
            try:
                environment, case['environment'] = derive_environment(portable, bootstrap, root, work, output, scenario, dict(os.environ))
                command = [str(root / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                           '--child', '--root', str(root), '--output', str(output / scenario)]
                case['command'] = command
                with (output / (scenario + '-child.log')).open('wb') as log:
                    result = subprocess.run(command, cwd=root, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=240)
                case['returncode'] = result.returncode
                child_report = json.loads((output / scenario / 'child-report.json').read_bytes())
                case['child_report_sha256'] = sha(output / scenario / 'child-report.json')
                if result.returncode != 0 or child_report.get('result') != 'passed': raise RuntimeError('Post-only OS probes failed; original result retained')
                case['result'] = 'passed'
            except Exception as error: case['error'] = f'{type(error).__name__}: {error}'
        if any(case['result'] != 'passed' for case in report['scenarios']): raise RuntimeError('One or more post-only scenarios failed')
        if original['result'] != 'passed': raise RuntimeError('New original acceptance failed; post probes cannot upgrade it')
        policy.validate_run_report(original, pins, PROFILE, RUNNER)
        report['result'] = 'passed'
    except Exception as error: report['error'] = f'{type(error).__name__}: {error}'
    finally:
        if frozen is not None:
            try:
                if snapshot(frozen_paths) != frozen: raise ValueError('Original attempt evidence changed during post probes')
                report['original_evidence_unchanged'] = True
            except Exception as error: report['after_errors'].append({'check': 'frozen-original-evidence', 'error': str(error)})
        if expected is not None:
            try:
                report['after_probe_tree'] = policy.verify_restored_tree(destination, expected)
                if report['after_probe_tree'] != original['restored_after_launcher']: raise ValueError('Restored tree changed')
            except Exception as error: report['after_errors'].append({'check': 'restored-tree', 'error': str(error)})
        if policy is not None and original is not None:
            try: report['original_inputs_after_probes'] = policy.verify_originals(work / 'original', original['original_files'])
            except Exception as error: report['after_errors'].append({'check': 'original-ZIP-sidecars', 'error': str(error)})
        if volume is not None:
            try:
                transport.verify_transport(volume)
                report['volumes_after_probes_verified'] = True
            except Exception as error: report['after_errors'].append({'check': 'volumes', 'error': str(error)})
        if report['after_errors']: report['result'] = 'failed'
        write_json(output / 'post-probe-report.json', report)
    return 0 if report['result'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
