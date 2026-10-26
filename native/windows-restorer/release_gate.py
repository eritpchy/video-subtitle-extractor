#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline future-release handoff: build once, test the SAME bytes on two Servers.

No workflow, download, publication, version change or synthetic receipt. Inputs
must come from the separately accepted final ZIP. Reports are local CI evidence,
not signed attestations, client-Windows certification or GPU execution proof.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
RUNNERS = {'windows-2022': 20348, 'windows-2025': 26100}
SCENARIOS = {'clean-host-path', 'contaminated-host-environment'}
RESERVE = 4 * 1024 ** 3
MAX_JSON = 32 * 1024 ** 2
MAX_SOURCE = 256 * 1024 ** 2


def local_helper(name):
    """Load only this directory's helpers, never a process-global short name.

    Gate APIs also run in shared Python processes (e.g. with native OCR tests,
    which have their own build.py). Do not consult or replace sys.modules['build']
    or any private-name cache: each call executes the exact local source file.
    The small allowlist prevents this helper becoming an arbitrary import API.
    """
    require(name in ('build', 'generate_metadata', 'source_bundle'), 'Unknown restorer helper')
    path = plain(HERE / (name + '.py'))
    spec = importlib.util.spec_from_file_location('_vse_windows_restorer_gate_' + name, path)
    require(spec is not None and spec.loader is not None, 'Missing local restorer loader')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def plain(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            f'Expected plain {"directory" if directory else "file"}: {path}')
    return path


def file_record(path):
    path = plain(path)
    return {'bytes': path.stat().st_size, 'sha256': sha(path)}


def read_json(path):
    path = plain(path)
    require(path.stat().st_size <= MAX_JSON, 'Oversized JSON evidence')
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    result = json.loads(path.read_bytes(), object_pairs_hook=pairs)
    require(isinstance(result, dict), 'Expected JSON object')
    return result


def write_json(path, document):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(document, stream, indent=2, ensure_ascii=True)
        stream.write('\n')


def read_bound_report(path, expected_sha256):
    require(re.fullmatch('[0-9a-f]{64}', expected_sha256) is not None,
            'Independent original CI report SHA required')
    before = file_record(path)
    require(before['sha256'] == expected_sha256, 'Original CI report SHA mismatch')
    document = read_json(path)
    require(file_record(path) == before, 'Original CI report changed during reading')
    return document, before


def finalize_report(path, report, failure, checks):
    """All after-checks run on success AND failure; never hide primary error."""
    errors = []
    for label, check in checks:
        try: report[label] = check()
        except Exception as error: errors.append({'check': label, 'error': f'{type(error).__name__}: {error}'})
    report['post_run_errors'] = errors
    if failure is not None: report['error'] = f'{type(failure).__name__}: {failure}'
    report['result'] = 'passed' if failure is None and not errors else 'failed'
    write_json(path, report)
    if failure is not None: raise failure
    require(not errors, 'Post-run input integrity checks failed; see preserved report')


def unchanged_files(records):
    require(all(file_record(Path(path)) == row for path, row in records.items()), 'Original input bytes changed')
    return {'result': 'passed', 'files': records}


def tree_inventory(root):
    root = plain(root, directory=True)
    rows = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            info = path.lstat()
            require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                    'Linked/reparse tree member')
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                rows[relative] = {'kind': 'directory', 'size': 0}
            else:
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Special/hardlinked tree member')
                rows[relative] = {'kind': 'file', 'size': info.st_size, 'sha256': sha(path)}
    return rows


def expected_tree(document):
    rows = {}
    for row in document['validated_zip_entries']:
        name = row['name'].rstrip('/')
        rows[name] = {key: row[key] for key in ('kind', 'size', 'sha256') if key in row}
        components = name.split('/')
        for at in range(1, len(components)):
            parent = '/'.join(components[:at])
            require(parent not in rows or rows[parent]['kind'] == 'directory', 'Inventory prefix conflict')
            rows.setdefault(parent, {'kind': 'directory', 'size': 0})
    return rows


def verify_tree(root, expected):
    actual = tree_inventory(root)
    require(actual == expected, 'Restored full SHA inventory differs (missing, extra, type or bytes)')
    return {'result': 'passed', 'entries': len(actual),
            'inventory_sha256': hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'all_file_sha256_checked': True, 'no_extra_or_reparse_entries': True}


def require_server(runner):
    require(runner in RUNNERS and os.name == 'nt' and platform.machine().lower() in ('amd64', 'x86_64'),
            'Run only on native x64 Server 2022/2025')
    version = sys.getwindowsversion()
    require(version.product_type == 3 and version.build == RUNNERS[runner], 'Runner label does not match native OS')
    return {'runner': runner, 'build': version.build, 'product_type': version.product_type}


def require_free(path, required):
    free = shutil.disk_usage(path).free
    require(free >= required, f'Insufficient actual free space: required {required}, available {free}')
    return {'required_bytes': required, 'available_bytes': free}


def validate_build(record):
    build = local_helper('build')
    build.assert_distributable_record(record)
    link = record.get('restricted_zlib', {}).get('final_link_map', {})
    require(link.get('required_inflate_symbols_present') is True
            and link.get('excluded_gzwrite_deflate_crc_archive_symbols_absent') is True,
            'Production raw-inflate symbol closure missing')
    require(record.get('schema') == 2 and record.get('result') == 'built-not-client-certified'
            and record.get('client_restore_verified') is False and record.get('gpu_inference_verified') is False,
            'Not an original production distribution build record')
    require(record.get('git', {}).get('dirty') is False and record.get('git_after_build') == record.get('git'),
            'Final candidate requires unchanged clean build-source revision')


def validate_identity(identity, commit, version, profile):
    require(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'Exact accepted application commit required')
    require((identity.get('commit'), identity.get('version'), identity.get('build_profile')) == (commit, version, profile)
            and identity.get('platform') == 'windows' and identity.get('arch') == 'x64'
            and identity.get('ocr_backend') == 'paddle' and profile in ('paddle-cuda11.8', 'paddle-cuda12.6'),
            'Accepted Windows CUDA identity does not match requested final bytes')


def bind_distribution(manifest, distribution, output, *, commit, version, profile, archive_sha256,
                      receipt_sha256, checksum_sha256):
    """Called after ONE build.main invocation; does not compile another EXE."""
    from scripts.release_volume_transport import verify_transport
    document = verify_transport(manifest)
    validate_identity(document['archive']['identity'], commit, version, profile)
    require(document['archive']['sha256'] == archive_sha256, 'Accepted original ZIP hash mismatch')
    require(document['receipt']['sha256'] == receipt_sha256 and document['checksum']['sha256'] == checksum_sha256,
            'Original independently anchored receipt/checksum mismatch')
    record = read_json(distribution / 'build-record.json')
    validate_build(record)
    require(record['exe_sha256'] == sha(distribution / 'restore.exe')
            and record['exe_bytes'] == (distribution / 'restore.exe').stat().st_size,
            'Distribution EXE differs from the one production build')
    require(record['archive_transport_manifest_sha256'] == sha(manifest), 'Build used a different transport manifest')
    source = record['source_companion']
    require(source['name'] == 'restorer-source.zip' and file_record(distribution / source['name']) ==
            {'bytes': source['bytes'], 'sha256': source['sha256']}, 'Source companion binding missing')
    allowed = {'restore.exe', 'restorer-source.zip', 'build-record.json', 'notices'}
    require({p.name for p in distribution.iterdir()} == allowed, 'Unexpected distribution root entry')
    require({p.name: sha(plain(p)) for p in plain(distribution / 'notices', directory=True).iterdir()} == record['notices'],
            'Distribution license notices changed')
    handoff = {'schema': 1, 'scope': 'final-zip-one-build-restorer-handoff-not-release-authorization',
               'archive': document['archive'], 'transport_manifest': file_record(manifest),
               'distribution': tree_inventory(distribution), 'exe_sha256': record['exe_sha256'],
               'source_zip_sha256': source['sha256'], 'build_source_commit': record['git']['commit'],
               'original_receipt': document['receipt'], 'original_checksum': document['checksum'],
               'gpu_inference_verified': False, 'client_windows_10_11_verified': False}
    if output is not None: write_json(output, handoff)
    return handoff


def prepare(args):
    require_server(args.runner)
    require(args.acknowledge_redistribution_license, 'Distributor must explicitly confirm existing redistribution rights')
    paths = (args.archive, args.archive.with_suffix('.zip.acceptance.json'), args.archive.with_suffix('.zip.sha256'))
    pins = (args.archive_sha256, args.receipt_sha256, args.checksum_sha256)
    for path, pin in zip(paths, pins):
        require(re.fullmatch('[0-9a-f]{64}', pin) is not None and sha(plain(path)) == pin,
                'Original accepted ZIP/receipt/checksum differs from independent CI artifact SHA')
    from scripts.release_volume_transport import create_transport, verify_transport
    work = args.work.absolute(); work.mkdir(parents=False, exist_ok=False)
    originals = {str(path): file_record(path) for path in paths}
    # This producer does not restore a payload. Reserve bounds its volume copy,
    # fixed header, compilation and source companion; consumers budget separately.
    report = {'schema': 1, 'scope': 'one-production-build-producer-not-release-authorization', 'result': 'failed',
              'original_inputs': originals, 'gpu_inference_verified': False}
    failure = None; manifest = None
    try:
        report['budget'] = require_free(work, args.archive.stat().st_size + RESERVE)
        manifest = create_transport(args.archive, work / 'volumes')
        document = verify_transport(manifest)
        validate_identity(document['archive']['identity'], args.commit, args.version, args.profile)
        command = [sys.executable, '-B', str(HERE / 'build.py'), '--manifest', str(manifest),
                   '--output', str(work / 'build'), '--notices-directory', str(args.notices.absolute()),
                   '--acknowledge-redistribution-license']
        report['command'] = command
        write_json(work / 'producer-command.json', {'command': command, 'budget': report['budget']})
        with (work / 'producer.log').open('xb') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        unchanged_files(originals)
        report['handoff'] = bind_distribution(manifest, work / 'build/distribution', None, commit=args.commit,
            version=args.version, profile=args.profile, archive_sha256=args.archive_sha256,
            receipt_sha256=args.receipt_sha256, checksum_sha256=args.checksum_sha256)
    except Exception as error: failure = error
    checks = [('original_inputs_after', lambda: unchanged_files(originals))]
    if manifest is not None:
        checks.append(('transport_after', lambda: verify_transport(manifest)))
    finalize_report(work / 'producer-report.json', report, failure, checks)
    # No consumable handoff is published until every original/volume after-check
    # passed, including when an earlier build step failed.
    write_json(work / 'handoff.json', report['handoff'])


def load_handoff(path, expected_sha256, distribution, manifest):
    require(re.fullmatch('[0-9a-f]{64}', expected_sha256) is not None and sha(plain(path)) == expected_sha256,
            'Handoff must match independently supplied producer artifact SHA')
    handoff = read_json(path)
    require(handoff.get('schema') == 1 and handoff.get('scope') == 'final-zip-one-build-restorer-handoff-not-release-authorization'
            and handoff.get('gpu_inference_verified') is False and handoff.get('client_windows_10_11_verified') is False,
            'Invalid handoff scope')
    require(file_record(manifest) == handoff['transport_manifest'] and tree_inventory(distribution) == handoff['distribution'],
            'Transferred production distribution or manifest changed')
    require(sha(distribution / 'restore.exe') == handoff['exe_sha256']
            and sha(distribution / 'restorer-source.zip') == handoff['source_zip_sha256'], 'Handoff binary/source identity mismatch')
    from scripts.release_volume_transport import verify_transport
    document = verify_transport(manifest, include_entries=True)
    require(document['archive'] == handoff['archive'], 'Transport belongs to another accepted ZIP')
    require(document['receipt'] == handoff['original_receipt'] and document['checksum'] == handoff['original_checksum'],
            'Original accepted receipt/checksum identity changed')
    validate_build(read_json(distribution / 'build-record.json'))
    return handoff, document


def validate_consumer(report, handoff, runner):
    require(report.get('schema') == 1 and report.get('scope') == 'same-exe-final-zip-server-restore'
            and report.get('result') == 'passed' and report.get('runner') == runner
            and report.get('native_os') == {'runner': runner, 'build': RUNNERS[runner], 'product_type': 3}
            and report.get('exe_sha256') == handoff['exe_sha256']
            and report.get('source_zip_sha256') == handoff['source_zip_sha256']
            and report.get('archive_sha256') == handoff['archive']['sha256']
            and report.get('compile_performed') is False and report.get('restore_returncode') == 0
            and report.get('post_run_errors') == [] and report.get('inputs_unchanged_verified') is True
            and report.get('gpu_inference_verified') is False and report.get('client_windows_10_11_verified') is False,
            'Incomplete/different-byte Server restoration report')
    before = report.get('restored_before_launcher', {})
    require(before.get('result') == 'passed' and before.get('all_file_sha256_checked') is True
            and before.get('no_extra_or_reparse_entries') is True and before == report.get('restored_after_launcher')
            and type(before.get('entries')) is int and before['entries'] > 0
            and re.fullmatch('[0-9a-f]{64}', before.get('inventory_sha256', '')) is not None,
            'Full restored tree before/after proof missing')
    require(isinstance(report.get('launcher_raw_inventory'), dict) and bool(report['launcher_raw_inventory'])
            and set(report.get('raw_logs', {})) == {'restore.stdout.log', 'restore.stderr.log', 'launcher.log'},
            'Raw launcher evidence inventory and native/launcher log hashes are required')
    launcher = report.get('launcher', {})
    cases = launcher.get('scenarios', [])
    require(launcher.get('result') == 'passed' and launcher.get('commit') == handoff['archive']['identity']['commit']
            and launcher.get('archive_extraction_used') is False and launcher.get('gpu_inference_verified') is False
            and launcher.get('application_source_after', {}).get('result') == 'passed' and launcher.get('post_run_errors') == []
            and len(cases) == 2 and {case.get('scenario') for case in cases} == SCENARIOS
            and all(case.get('result') == 'passed' and case.get('returncode') == 0
                    and case.get('audit', {}).get('passed') is True and case.get('audit', {}).get('errors') == [] for case in cases),
            'Both original-launcher clean/contaminated acceptance scenarios are required')


def consume(args):
    native_os = require_server(args.runner)
    handoff, document = load_handoff(args.handoff, args.handoff_sha256, args.distribution, args.manifest)
    work = args.work.absolute(); work.mkdir(parents=False, exist_ok=False)
    expected = expected_tree(document)
    report = {'schema': 1, 'scope': 'same-exe-final-zip-server-restore', 'result': 'failed',
              'runner': args.runner, 'native_os': native_os, 'handoff_sha256': args.handoff_sha256,
              'exe_sha256': handoff['exe_sha256'], 'source_zip_sha256': handoff['source_zip_sha256'],
              'archive_sha256': handoff['archive']['sha256'], 'compile_performed': False,
              'gpu_inference_verified': False, 'client_windows_10_11_verified': False}
    destination = work / 'restored Unicode 中文 &!()'; failure = None
    try:
        report['budget'] = require_free(work, document['archive']['size'] + document['archive']['uncompressed_bytes']
                                       + 65536 * len(expected) + RESERVE)
        destination.mkdir()
        command = [str(args.distribution.absolute() / 'restore.exe'), '--source', str(args.manifest.absolute().parent),
                   '--destination-parent', str(destination), '--non-interactive']
        report['restore_command'] = command
        with (work / 'restore.stdout.log').open('xb') as out, (work / 'restore.stderr.log').open('xb') as err:
            restored = subprocess.run(command, stdout=out, stderr=err, timeout=1800)
        report['restore_returncode'] = restored.returncode; restored.check_returncode()
        report['restored_before_launcher'] = verify_tree(destination, expected)
        command = [sys.executable, '-I', '-B', '-X', 'utf8', str(HERE / 'restored_launcher_audit.py'),
                   '--application-source', str(args.application_source.absolute()), '--application-commit', document['archive']['identity']['commit'],
                   '--root', str(destination / document['archive']['root']), '--output', str(work / 'launcher-evidence'), '--runner', args.runner]
        report['launcher_command'] = command
        with (work / 'launcher.log').open('xb') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=11000, check=True)
        report['launcher'] = read_json(work / 'launcher-evidence/restored-launcher-report.json')
    except Exception as error:
        failure = error; report['error'] = f'{type(error).__name__}: {error}'
    finally:
        errors = []
        try:
            report['restored_after_launcher'] = verify_tree(destination, expected)
        except Exception as error:
            errors.append({'check': 'restored-tree-after', 'error': str(error)})
        try:
            load_handoff(args.handoff, args.handoff_sha256, args.distribution, args.manifest)
            report['inputs_unchanged_verified'] = True
        except Exception as error:
            errors.append({'check': 'distribution-and-volumes-after', 'error': str(error)})
        report['post_run_errors'] = errors
        if failure is None and not errors:
            report['result'] = 'passed'
        if failure is not None or errors:
            report['result'] = 'failed'
        if failure is not None: report.setdefault('error', f'{type(failure).__name__}: {failure}')
        if (work / 'launcher-evidence').is_dir():
            try: report['launcher_raw_inventory'] = tree_inventory(work / 'launcher-evidence')
            except Exception as error:
                report['post_run_errors'].append({'check': 'launcher-raw-evidence', 'error': str(error)})
                report['result'] = 'failed'
        try: report['raw_logs'] = {path.name: file_record(path) for path in work.glob('*.log')}
        except Exception as error:
            report['post_run_errors'].append({'check': 'raw-logs', 'error': str(error)})
            report['result'] = 'failed'
        if report['result'] == 'passed':
            try: validate_consumer(report, handoff, args.runner)
            except Exception as error:
                report['result'] = 'failed'; report['error'] = f'{type(error).__name__}: {error}'
        write_json(work / 'consumer-report.json', report)
    require(report['result'] == 'passed', 'Server restoration/launcher gate failed; original error and after-checks retained')


def extract_source(archive, output):
    """Extract only the hash-bound source companion, never the application ZIP."""
    _component = local_helper('generate_metadata')._component
    verify_extracted_sources = local_helper('source_bundle').verify_extracted_sources
    with zipfile.ZipFile(plain(archive)) as zipped:
        entries = zipped.infolist(); names = set(); total = 0
        require(0 < len(entries) <= 10000, 'Source companion entry bound exceeded')
        for entry in entries:
            name = entry.orig_filename
            require(name == entry.filename and str(PurePosixPath(name)) == name and not name.startswith('/')
                    and not entry.is_dir() and not entry.flag_bits & 1
                    and stat.S_IFMT(entry.external_attr >> 16) in (0, stat.S_IFREG), 'Unsafe source companion entry')
            for component in name.split('/'): _component(component)
            require(name.casefold() not in names, 'Aliased source companion entry'); names.add(name.casefold())
            total += entry.file_size
            require(total <= MAX_SOURCE and entry.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), 'Source companion bounds exceeded')
        require_free(output.parent, total + RESERVE)
        output.mkdir(parents=False, exist_ok=False)
        for entry in entries:
            target = output / entry.filename; target.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(entry) as source, target.open('xb') as destination:
                shutil.copyfileobj(source, destination, length=1024 ** 2)
    return verify_extracted_sources(output)


def rebuild_source(args):
    require_server(args.runner)
    require(args.acknowledge_redistribution_license, 'Distributor must explicitly confirm existing redistribution rights')
    handoff, _ = load_handoff(args.handoff, args.handoff_sha256, args.distribution, args.manifest)
    work = args.work.absolute(); work.mkdir(parents=False, exist_ok=False)
    source = work / 'source'; failure = None; extracted = False
    report = {'schema': 1, 'result': 'failed',
        'scope': 'independent-source-companion-rebuild-not-bit-identity-or-restore-proof', 'runner': args.runner,
        'handoff_sha256': args.handoff_sha256, 'source_zip_sha256': handoff['source_zip_sha256'],
        'original_exe_sha256': handoff['exe_sha256']}
    # -I removes script-directory imports; rebuild's self-contained entry adds
    # only its own verified source directory, never the producer checkout.
    try:
        manifest = extract_source(args.distribution / 'restorer-source.zip', source); extracted = True
        require(manifest['original_exe_sha256'] == handoff['exe_sha256'], 'Source companion belongs to another EXE')
        command = [sys.executable, '-I', '-S', '-B', str(source / 'native/windows-restorer/rebuild.py'),
                   '--source-root', str(source), '--output', str(work / 'rebuilt'), '--acknowledge-redistribution-license']
        write_json(work / 'rebuild-command.json', {'command': command})
        with (work / 'rebuild.log').open('xb') as log:
            subprocess.run(command, cwd=work, stdout=log, stderr=subprocess.STDOUT, check=True)
        rebuilt = read_json(work / 'rebuilt/rebuild-record.json')
        build = local_helper('build')
        build.assert_distributable_record(rebuilt)
        require(rebuilt['original_exe_sha256'] == handoff['exe_sha256']
                and sha(work / 'rebuilt/restore.exe') == rebuilt['exe_sha256'], 'Independent rebuild output hash mismatch')
        report.update(rebuilt_exe_sha256=rebuilt['exe_sha256'], bit_identical_to_original=rebuilt['bit_identical_to_original'],
                      rebuild_record=rebuilt, raw_log=file_record(work / 'rebuild.log'))
    except Exception as error: failure = error
    checks = [('inputs_after', lambda: {'result': 'passed', 'handoff': load_handoff(
        args.handoff, args.handoff_sha256, args.distribution, args.manifest)[0]})]
    if extracted:
        verify_extracted_sources = local_helper('source_bundle').verify_extracted_sources
        checks.append(('source_after', lambda: verify_extracted_sources(source)))
    finalize_report(work / 'source-rebuild-report.json', report, failure, checks)


def combine(handoff, reports, rebuilt):
    require(len(reports) == 2 and {row.get('runner') for row in reports} == set(RUNNERS), 'Exactly two distinct Server reports required')
    for row in reports: validate_consumer(row, handoff, row['runner'])
    require(reports[0]['restored_before_launcher'] == reports[1]['restored_before_launcher'],
            'The same accepted ZIP must produce the same full payload inventory on both Servers')
    require(rebuilt.get('schema') == 1 and rebuilt.get('result') == 'passed'
            and rebuilt.get('scope') == 'independent-source-companion-rebuild-not-bit-identity-or-restore-proof'
            and rebuilt.get('runner') in RUNNERS and rebuilt.get('original_exe_sha256') == handoff['exe_sha256']
            and rebuilt.get('source_zip_sha256') == handoff['source_zip_sha256']
            and rebuilt.get('post_run_errors') == [] and rebuilt.get('inputs_after', {}).get('result') == 'passed'
            and rebuilt.get('source_after', {}).get('kind') == 'vse-native-restorer-exact-source'
            and rebuilt.get('source_after', {}).get('original_exe_sha256') == handoff['exe_sha256'],
            'Independent exact-source rebuild evidence missing')
    require(re.fullmatch('[0-9a-f]{64}', rebuilt.get('rebuilt_exe_sha256', '')) is not None
            and type(rebuilt.get('bit_identical_to_original')) is bool
            and rebuilt['bit_identical_to_original'] == (rebuilt['rebuilt_exe_sha256'] == handoff['exe_sha256']), 'Rebuild comparison is not factual')
    record = rebuilt.get('rebuild_record', {})
    build = local_helper('build')
    build.assert_distributable_record(record)
    link = record.get('restricted_zlib', {}).get('final_link_map', {})
    require(record.get('scope') == 'fixed-header-source-rebuild-not-client-certification'
            and record.get('original_exe_sha256') == handoff['exe_sha256']
            and record.get('exe_sha256') == rebuilt['rebuilt_exe_sha256']
            and link.get('required_inflate_symbols_present') is True
            and link.get('excluded_gzwrite_deflate_crc_archive_symbols_absent') is True,
            'Independent rebuild production symbol gates missing')
    require(len({row.get('handoff_sha256') for row in [*reports, rebuilt]}) == 1
            and re.fullmatch('[0-9a-f]{64}', rebuilt.get('handoff_sha256', '')) is not None,
            'Evidence reports belong to different handoffs')
    return {'schema': 1, 'result': 'passed', 'scope': 'same-byte-two-server-restore-and-source-rebuild-not-release-authorization',
            'archive_sha256': handoff['archive']['sha256'], 'exe_sha256': handoff['exe_sha256'],
            'source_zip_sha256': handoff['source_zip_sha256'], 'runners': sorted(RUNNERS),
            'gpu_inference_verified': False, 'client_windows_10_11_verified': False,
            'bit_identical_rebuild_verified': rebuilt['bit_identical_to_original']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    producer = sub.add_parser('prepare', help='Accepted final ZIP → one production distribution; no restore/publication')
    for name in ('archive', 'notices', 'work'): producer.add_argument('--' + name, type=Path, required=True)
    for name in ('archive-sha256', 'receipt-sha256', 'checksum-sha256', 'commit', 'version', 'profile'):
        producer.add_argument('--' + name, required=True)
    producer.add_argument('--runner', choices=tuple(RUNNERS), required=True)
    producer.add_argument('--acknowledge-redistribution-license', action='store_true')
    for action in ('consume', 'rebuild-source', 'combine'):
        command = sub.add_parser(action)
        for name in ('handoff', 'distribution', 'manifest'): command.add_argument('--' + name, type=Path, required=True)
        command.add_argument('--handoff-sha256', required=True)
        if action == 'combine':
            command.add_argument('--consumer-report', type=Path, action='append', required=True)
            command.add_argument('--consumer-report-sha256', action='append', required=True)
            command.add_argument('--rebuild-report', type=Path, required=True)
            command.add_argument('--rebuild-report-sha256', required=True)
            command.add_argument('--output', type=Path, required=True)
        else:
            command.add_argument('--work', type=Path, required=True)
            command.add_argument('--runner', choices=tuple(RUNNERS), required=True)
            if action == 'consume': command.add_argument('--application-source', type=Path, required=True)
            else: command.add_argument('--acknowledge-redistribution-license', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'prepare': prepare(args)
    elif args.action == 'consume': consume(args)
    elif args.action == 'rebuild-source': rebuild_source(args)
    else:
        handoff, _ = load_handoff(args.handoff, args.handoff_sha256, args.distribution, args.manifest)
        require(len(args.consumer_report) == len(args.consumer_report_sha256) == 2, 'Two report paths with two independent hashes required')
        pairs = [read_bound_report(path, pin) for path, pin in zip(args.consumer_report, args.consumer_report_sha256)]
        reports = [row[0] for row in pairs]
        rebuilt, rebuild_pin = read_bound_report(args.rebuild_report, args.rebuild_report_sha256)
        require(all(row.get('handoff_sha256') == args.handoff_sha256 for row in [*reports, rebuilt]), 'Evidence belongs to another handoff')
        result = combine(handoff, reports, rebuilt)
        result['original_ci_reports'] = {'consumers': [{'runner': row['runner'], **pin} for row, pin in pairs], 'source_rebuild': rebuild_pin}
        result['handoff_sha256'] = args.handoff_sha256
        write_json(args.output, result)
    return 0


if __name__ == '__main__': raise SystemExit(main())
