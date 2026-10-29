#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Independent Server phase controls, not restored-launcher acceptance or old failure replay."""
from __future__ import annotations
import argparse, ast, ctypes
from datetime import datetime, timezone
import hashlib, importlib.util, json, os
from pathlib import Path
import struct, subprocess, sys, time
BASELINE='c45c0b03d3eef548caa69b8453e2aa80376098e9'
SOURCE_SHA='750f7a0543b687215889c3b69b5a401734716df5a2c3316dea8b509639a38fc3'
TIMEOUT=60
STAGES=('enter','before_cim','after_cim','before_registry','after_registry','before_json','after_json')
RUNNERS={'windows-2022':20348,'windows-2025':26100}
SAFE_ENV=frozenset(('SYSTEMROOT WINDIR COMSPEC TEMP TMP NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE '
    'PROGRAMFILES PROGRAMFILES(X86) PROGRAMDATA SYSTEMDRIVE USERPROFILE USERNAME USERDOMAIN OS PATH').split())


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()

def write_json(path, document):
    with Path(path).open('x', encoding='utf-8', newline='\n') as output:
        json.dump(document, output, indent=2, ensure_ascii=False); output.write('\n')

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

def original_command(path):
    """Read the uniquely pinned source; never execute/eval extracted AST."""
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ValueError('OS capture source is not exact selected C45 LF bytes')
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
        for name in ('ProductName', 'DisplayVersion', 'EditionID', 'UBR', 'CurrentBuildNumber', 'CurrentMajorVersionNumber', 'CurrentMinorVersionNumber', 'InstallationType'):
            try:
                value, kind = winreg.QueryValueEx(key, name)
                if kind not in (winreg.REG_SZ, winreg.REG_DWORD) or not isinstance(value, (str, int)):
                    raise ValueError('Unexpected OS registry value type')
                record['values'][name] = {'present': True, 'type': kind, 'value': value}
            except FileNotFoundError:
                record['values'][name] = {'present': False}
    return record

def native_control(module, runner):
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
    if record['RtlGetVersion']['build'] != RUNNERS[runner] or value.build != RUNNERS[runner] or version.product_type != 3 or value.product_type != 3:
        raise ValueError('Native OS controls differ from authorized Server runner')
    if record['architecture']['host']!='x64' or record['architecture']['process']!='x64' or record['architecture']['emulated'] or struct.calcsize('P')!=8:
        raise ValueError('Actual native x64 interpreter required')
    return record


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--runner',choices=tuple(RUNNERS),required=True)
    args=parser.parse_args(argv)
    if os.name!='nt' or sys.platform!='win32':raise RuntimeError('Native Windows Server only; no simulation')
    source=args.source.resolve(strict=True);output=args.output.absolute();output.mkdir(exist_ok=False)
    report={'scope':'independent-new-Server-controls-not-C45-acceptance-or-old-instantaneous-replay','result':'failed',
      'application_commit':BASELINE,'source_sha256':SOURCE_SHA,'runner':args.runner,'original_run':40819426925,
      'old_failure':'entire original PowerShell capture exceeded60seconds; internal statement unknown',
      'no_candidate_modified':True,'no_GUI_or_process_enumeration':True,'probes':[],'errors':[]}
    try:
        head=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
        dirty=subprocess.check_output(['git','-C',str(source),'status','--porcelain'],text=True).strip()
        if head!=BASELINE or dirty:raise RuntimeError('Exact clean selected C45 checkout required')
        script=source/'scripts/windows_verification.py';command=original_command(script)
        module=load('exact_selected_windows_verification',script)
        environment={k:v for k,v in os.environ.items() if k.upper() in SAFE_ENV}
        executable,identity=system_powershell(environment);report['PowerShell']=identity
        def validate(measured):
            module.validate_identity({'os':measured,'architecture':module.windows_architecture(),
                                      'python':{'pointer_bits':struct.calcsize('P')*8}},args.runner)
        for label,script_command,marked in (('original-control',command,False),('stage-marked',marked_command(command),True)):
            report['probes'].append(run_capture(executable,script_command,output,environment,output/label,marked,validate))
        report['native_numeric_and_registry_control']=native_control(module,args.runner)
        if sha(script)!=SOURCE_SHA:raise RuntimeError('Original source bytes changed')
        if any(row['result']!='passed' for row in report['probes']):raise RuntimeError('A control failed; raw partial streams retained')
        report['result']='passed'
        report['old_failure_reproduced']=False
        report['causal_claim']='No causal inference about the old host; original-control precedes marked-control on a new runner'
    except Exception as error:
        report['errors'].append(type(error).__name__+': '+str(error))
        raise
    finally:write_json(output/'diagnostic-report.json',report)

if __name__=='__main__':main()
