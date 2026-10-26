# SPDX-License-Identifier: Apache-2.0
"""Standalone Server-only, 48-case rename observations; never release acceptance.

Use the already selected MSVC x64 environment. The new work directory contains
only synthetic trees, compiler outputs and evidence. No cleanup or overwrite is
performed. Each case runs in a separate process, so an old-buffer crash does not
hide later candidate observations. Failed expectations remain overall nonzero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess

HERE = Path(__file__).resolve().parent
MAX_CASE_LOG_BYTES = 2 * 1024 * 1024
MODES = ('relative-exact-buffer', 'relative-nul-buffer', 'absolute-extended-nul-buffer')


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def definition(index):
    if type(index) is not int or not 0 <= index < 48:
        raise ValueError('Case index must be an integer in 0..47')
    return {'case_index': index, 'mode': MODES[index // 16], 'existing_target': bool(index & 1),
            'long_parent': bool(index & 2), 'deep_child': bool(index & 4), 'unicode': bool(index & 8)}


def validate_record(record, expected, returncode):
    if not isinstance(record, dict) or any(type(record.get(key)) is not type(value) or record.get(key) != value for key, value in expected.items()):
        raise ValueError('Native record does not match requested independent case')
    if record.get('schema') != 1 or record.get('client_restore_verified') is not False:
        raise ValueError('Invalid diagnostic-only native record')
    if record.get('scope') != 'synthetic-rename-api-observation-not-production-or-client-certification':
        raise ValueError('Unexpected native report scope')
    if type(record.get('expectation_met')) is not bool:
        raise ValueError('Missing native outcome')
    if record['expectation_met'] != (returncode == 0):
        raise ValueError('Native exit code and evidence disagree')
    if record['expectation_met']:
        for key in ('ancestor_identities_unchanged', 'source_marker_bytes_unchanged', 'source_and_marker_identities_unchanged', 'existing_target_preserved'):
            if record.get(key) is not True:
                raise ValueError('Native success lacks identity/content evidence')
        if record.get('replace_if_exists') is not False:
            raise ValueError('Native diagnostic must prohibit replacement')
        for prefix in ('source', 'marker', 'destination_parent'):
            before = record.get(prefix + '_identity_before')
            after = record.get(prefix + '_identity_after')
            if not isinstance(before, dict) or before != after:
                raise ValueError('Native identity changed or is missing')
            if set(before) != {'volume_serial', 'file_index_high', 'file_index_low'} or any(type(value) is not int or not 0 <= value <= 0xffffffff for value in before.values()):
                raise ValueError('Malformed Win32 identity')
        if expected['existing_target']:
            if record.get('rename_returned_success') is not False or record.get('source_present_after') is not True:
                raise ValueError('Existing target was not safely refused')
            for prefix in ('existing_target', 'existing_sentinel'):
                if not isinstance(record.get(prefix + '_identity_before'), dict) or record.get(prefix + '_identity_before') != record.get(prefix + '_identity_after'):
                    raise ValueError('Existing target identity changed or is missing')
        elif record.get('rename_returned_success') is not True or record.get('source_present_after') is not False or record.get('target_present_after') is not True:
            raise ValueError('Rename success lacks source/target transition')
    return record


def aggregate(rows):
    complete = len(rows) == 48 and [row.get('case_index') for row in rows] == list(range(48))
    return {'all_cases_collected': complete,
            'all_expectations_met': complete and all(row.get('expectation_met') is True for row in rows),
            'observed_expectations_met': sum(row.get('expectation_met') is True for row in rows),
            'observed_expectations_not_met': sum(row.get('expectation_met') is not True for row in rows)}


def run_cases(executable, work, rows):
    cases = work / 'cases'; cases.mkdir()
    for index in range(48):
        expected = definition(index)
        stdout, stderr = work / f'case-{index:02d}.stdout.log', work / f'case-{index:02d}.stderr.log'
        command = [str(executable), '--new-root', str(cases / f'case-{index:02d}'), '--case', str(index)]
        row = {**expected, 'expectation_met': False, 'command': command}
        try:
            with stdout.open('xb') as out, stderr.open('xb') as err:
                result = subprocess.run(command, cwd=work, stdout=out, stderr=err, timeout=60)
            row['exit_code'] = result.returncode
            if stdout.stat().st_size > MAX_CASE_LOG_BYTES or stderr.stat().st_size > MAX_CASE_LOG_BYTES:
                raise ValueError('Native case output exceeds diagnostic bound')
            document = json.loads(stdout.read_bytes())
            validate_record(document, expected, result.returncode)
            row.update(document)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            row.update(expectation_met=False, collector_error=f'{type(error).__name__}: {error}')
        finally:
            for label, path in (('stdout', stdout), ('stderr', stderr)):
                if path.is_file():
                    row[label] = {'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha(path)}
            rows.append(row)
        yield row


def ensure_new_local_work(path):
    path = path.absolute()
    if str(path).startswith('\\\\') or not path.drive or not path.parent.is_dir():
        raise ValueError('Use an ordinary drive-absolute new directory under an existing local parent')
    for ancestor in (path.parent, *path.parent.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked/reparse work ancestor rejected')
    path.mkdir(exist_ok=False)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-directory', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('This diagnostic requires native Windows x64 Server and MSVC')
    compiler = shutil.which('cl.exe')
    if not compiler:
        raise RuntimeError('Use the existing selected MSVC x64 environment; no dependency is downloaded')
    work = ensure_new_local_work(args.work_directory)
    source = HERE / 'rename_probe.cpp'
    core = HERE.parent / 'core.hpp'
    sources = {str(path): sha(path) for path in (source, core, Path(__file__))}
    source_copy = work / 'probe-source'; source_copy.mkdir()
    for path in (source, core, Path(__file__)):
        shutil.copyfile(path, source_copy / path.name)
    executable = work / 'rename-probe-SYNTHETIC-NOT-FOR-DISTRIBUTION.exe'
    command = [compiler, '/nologo', '/Bv', '/std:c++17', '/EHsc', '/MT', '/O1', '/W4', '/WX', '/utf-8',
               '/GS', '/guard:cf', '/Brepro', '/DUNICODE', '/D_UNICODE', '/D_WIN32_WINNT=0x0A00', str(source),
               f'/Fo{work / "rename-probe.obj"}', f'/Fe{executable}', '/link', '/INCREMENTAL:NO',
               '/DYNAMICBASE', '/NXCOMPAT', '/HIGHENTROPYVA', '/DEPENDENTLOADFLAG:0x800', '/MACHINE:X64',
               f'/MAP:{work / "rename-probe.map"}', '/MANIFEST:EMBED', "/MANIFESTUAC:level='asInvoker' uiAccess='false'"]
    rows = []
    report = {'schema': 1, 'scope': 'synthetic-rename-api-observations-not-restoration-acceptance',
              'os': platform.platform(), 'python': platform.python_version(), 'cases': rows,
              'source_sha256': sources, 'compiler': {'path': compiler, 'sha256': sha(Path(compiler))},
              'compile_command': command, 'client_restore_verified': False, 'result': 'incomplete'}
    report_path = work / 'rename-probe-report.json'
    try:
        compile_environment = dict(os.environ)
        compile_environment.pop('CL', None)
        compile_environment.pop('_CL_', None)
        with (work / 'compiler.log').open('xb') as log:
            built = subprocess.run(command, cwd=work, stdout=log, stderr=subprocess.STDOUT, timeout=300, env=compile_environment)
        report['compile_exit_code'] = built.returncode
        built.check_returncode()
        if any(sha(Path(path)) != expected for path, expected in sources.items()):
            raise RuntimeError('Probe source changed during compilation')
        report['exe_sha256'] = sha(executable)
        for _ in run_cases(executable, work, rows):
            report.update(aggregate(rows))
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
        report.update(aggregate(rows))
        report['result'] = 'expectations-met' if report['all_expectations_met'] else 'expectations-not-met'
    finally:
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
    return 0 if report.get('all_expectations_met') else 1


if __name__ == '__main__':
    raise SystemExit(main())
