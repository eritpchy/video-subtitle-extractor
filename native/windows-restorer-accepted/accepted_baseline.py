# SPDX-License-Identifier: Apache-2.0
"""Read-only acquisition and byte/tree checks for the explicitly pinned CI baseline.

Build-side Python only. No publication, customer installation, archive repacking,
GPU inference claim, host cleanup, or replacement of the original acceptance.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile

BASELINE = '3064320ec74e1aa71f3702c2ea966485f4b31e33'
RESERVE = 4 * 1024 ** 3
MAX_SIDECAR = 8 * 1024 ** 2


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique_json(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def load_pins(path):
    result = unique_json(Path(path).read_bytes())
    if (result.get('schema') != 1 or result.get('scope') != 'accepted-306-baseline-not-final-release'
            or result.get('repository') != 'eritpchy/video-subtitle-extractor' or result.get('commit') != BASELINE
            or result.get('run_id') != 40288916991 or result.get('run_attempt') != 1):
        raise ValueError('Not the authorized exact baseline/run/repository')
    rows = result.get('profiles')
    if not isinstance(rows, list) or len(rows) != 2 or {r.get('profile') for r in rows} != {'paddle-cuda11.8', 'paddle-cuda12.6'}:
        raise ValueError('Exactly both authorized CUDA profiles are required')
    for row in rows:
        accelerator = row['profile'].removeprefix('paddle-')
        if (row.get('archive_name') != f'vse-2.3.0-windows-x64-{accelerator}.zip' or
                row.get('artifact_name') != f'release-candidate-windows-x64-{row["profile"]}'):
            raise ValueError('Profile/archive/artifact identity mismatch')
        for name in ('artifact_id', 'artifact_bytes', 'archive_bytes', 'expanded_bytes'):
            if type(row.get(name)) is not int or not 0 < row[name] <= 2 ** 40: raise ValueError('Invalid bounded size/id')
        for name in ('artifact_sha256', 'archive_sha256', 'checksum_sha256'):
            if not isinstance(row.get(name), str) or not re.fullmatch('[0-9a-f]{64}', row[name]): raise ValueError('Missing exact SHA256')
    return result


def disk_budget(row, entry_count):
    if type(entry_count) is not int or not 1 <= entry_count <= 200000: raise ValueError('Invalid native inventory count')
    archive, expanded = row['archive_bytes'], row['expanded_bytes']
    native = archive + expanded + entry_count * 65536 + 64 * 1024 ** 2
    return {'original_zip_bytes': archive, 'parts_bytes': archive, 'merged_zip_bytes': archive,
            'expanded_bytes': expanded, 'native_entry_count': entry_count,
            'native_preflight_bytes': native, 'build_and_workload_reserve_bytes': RESERVE,
            'download_phase_extra_bytes': row['artifact_bytes'] + archive + RESERVE,
            'after_original_download_extra_bytes': archive + native + RESERVE,
            'peak_bytes_including_original': max(row['artifact_bytes'] + archive + RESERVE, 2 * archive + native + RESERVE),
            'scope': 'Conservative simultaneous files plus reserve, not a physical disk-exhaustion test'}


def require_free(directory, required, phase):
    available = shutil.disk_usage(directory).free
    record = {'phase': phase, 'available_bytes': available, 'required_additional_bytes': required}
    if available < required:
        error = RuntimeError(f'Insufficient CI free space at {phase}: {available} < {required}; no host cleanup attempted')
        error.disk_record = record
        raise error
    return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl): return None


def acquire_artifact(pins, row, destination, token):
    """Only a fixed GitHub API artifact; signed redirect never receives the token."""
    if not token: raise RuntimeError('Read-only Actions token required for the exact artifact')
    endpoint = 'https://api.github.com/repos/' + pins['repository'] + '/actions/artifacts/' + str(row['artifact_id'])
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(urllib.request.Request(endpoint, headers=headers), timeout=120) as response:
        raw = response.read(2 * 1024 ** 2 + 1)
        if len(raw) > 2 * 1024 ** 2: raise RuntimeError('Oversized GitHub artifact metadata')
        metadata = unique_json(raw)
    if (metadata.get('id') != row['artifact_id'] or metadata.get('name') != row['artifact_name']
            or metadata.get('size_in_bytes') != row['artifact_bytes'] or metadata.get('expired') is not False
            or metadata.get('digest') != 'sha256:' + row['artifact_sha256']
            or metadata.get('workflow_run', {}).get('id') != pins['run_id']
            or metadata.get('workflow_run', {}).get('head_sha') != pins['commit']):
        raise RuntimeError('GitHub artifact identity/size/digest/run differs from the fixed baseline')
    try:
        opener.open(urllib.request.Request(endpoint + '/zip', headers=headers), timeout=120)
    except urllib.error.HTTPError as error:
        if error.code != 302: raise
        location = error.headers.get('Location', '')
    else:
        raise RuntimeError('Expected signed GitHub artifact redirect')
    url = urllib.parse.urlsplit(location)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise RuntimeError('Unsafe artifact redirect')
    # Trust only GitHub's authenticated redirect, never a pin-file URL; never log
    # its short-lived query credential or forward Authorization to the blob host.
    digest = hashlib.sha256(); count = 0
    with urllib.request.urlopen(location, timeout=120) as response, Path(destination).open('xb') as output:
        while chunk := response.read(1024 ** 2):
            count += len(chunk)
            if count > row['artifact_bytes']: raise RuntimeError('Artifact exceeded its pinned size')
            digest.update(chunk); output.write(chunk)
        output.flush(); os.fsync(output.fileno())
    if count != row['artifact_bytes'] or digest.hexdigest() != row['artifact_sha256']:
        raise RuntimeError('Original artifact byte SHA/size mismatch')
    return {'id': metadata['id'], 'name': metadata['name'], 'run_id': pins['run_id'], 'commit': pins['commit'],
            'bytes': count, 'sha256': digest.hexdigest(), 'original_acceptance_not_yet_verified': True}


def extract_original(container, destination, row):
    """Unwrap exactly three pinned artifact members, never extract the payload ZIP."""
    if Path(container).stat().st_size != row['artifact_bytes'] or sha(container) != row['artifact_sha256']:
        raise ValueError('Artifact is not the pinned original')
    expected = {row['archive_name'], row['archive_name'] + '.sha256', row['archive_name'] + '.acceptance.json'}
    destination = Path(destination); destination.mkdir()
    result = {}
    with zipfile.ZipFile(container) as archive:
        entries = archive.infolist()
        if len(entries) != 3 or {entry.filename for entry in entries} != expected:
            raise ValueError('Artifact must contain exactly original ZIP, checksum and final acceptance sidecar')
        for entry in entries:
            mode = entry.external_attr >> 16
            if (entry.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or entry.flag_bits & 1
                    or entry.file_size > (row['archive_bytes'] if entry.filename == row['archive_name'] else MAX_SIDECAR)):
                raise ValueError('Unsupported/bounded artifact member')
            path = destination / entry.filename
            with archive.open(entry) as source, path.open('xb') as output:
                shutil.copyfileobj(source, output, length=1024 ** 2)
            result[entry.filename] = {'bytes': path.stat().st_size, 'sha256': sha(path)}
    if result[row['archive_name']] != {'bytes': row['archive_bytes'], 'sha256': row['archive_sha256']}:
        raise ValueError('Original package SHA/size mismatch')
    if result[row['archive_name'] + '.sha256']['sha256'] != row['checksum_sha256']:
        raise ValueError('Original checksum sidecar changed')
    return result


def inventory(document):
    result = {}
    for row in document['validated_zip_entries']:
        name = row['name'].rstrip('/')
        result[name] = {'kind': row['kind'], 'size': row['size'], **({'sha256': row['sha256']} if row['kind'] == 'file' else {})}
        components = name.split('/')
        for at in range(1, len(components)):
            parent = '/'.join(components[:at])
            if parent in result and result[parent]['kind'] != 'directory': raise ValueError('Inventory prefix conflict')
            result.setdefault(parent, {'kind': 'directory', 'size': 0})
    return result


def verify_restored_tree(parent, expected):
    """Independent full file SHA and exact directory/reparse/extra-entry audit."""
    parent = Path(parent)
    actual = {}
    for root, directories, files in os.walk(parent, followlinks=False):
        for name in directories + files:
            path = Path(root) / name; info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse/symlink in restored payload')
            relative = path.relative_to(parent).as_posix()
            if stat.S_ISDIR(info.st_mode): actual[relative] = {'kind': 'directory', 'size': 0}
            elif stat.S_ISREG(info.st_mode): actual[relative] = {'kind': 'file', 'size': info.st_size, 'sha256': sha(path)}
            else: raise ValueError('Special restored member')
    if actual != expected: raise ValueError('Restored full SHA inventory differs (missing, extra or changed entry)')
    encoded = json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()
    return {'result': 'passed', 'entries': len(actual), 'files': sum(r['kind'] == 'file' for r in actual.values()),
            'inventory_sha256': hashlib.sha256(encoded).hexdigest(), 'all_file_sha256_checked': True, 'no_extra_or_reparse_entries': True}


def verify_originals(directory, records):
    directory = Path(directory)
    if {path.name for path in directory.iterdir()} != set(records):
        raise ValueError('Original accepted input directory changed')
    for name, record in records.items():
        path = directory / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_size != record['bytes'] or sha(path) != record['sha256']):
            raise ValueError('Original accepted ZIP/sidecar bytes or type changed')
    return {'result': 'passed', 'files': len(records), 'original_bytes_unchanged': True}


def validate_run_report(report, pins, profile, runner):
    row = next(row for row in pins['profiles'] if row['profile'] == profile)
    if (report.get('schema') != 1 or report.get('result') != 'passed'
            or report.get('scope') != 'accepted-306-baseline-native-restore-not-final-release'
            or report.get('baseline_commit') != pins['commit'] or report.get('profile') != profile
            or report.get('runner') != runner or report.get('archive_sha256') != row['archive_sha256']
            or report.get('restore_returncode') != 0 or type(report.get('restore_returncode')) is not int
            or any(report.get(key) is not False for key in ('gpu_inference_verified', 'client_windows_10_11_verified',
                                                           'release_distribution_created', 'physical_disk_exhaustion_tested'))):
        raise ValueError('Baseline report identity/result/scope differs')
    artifact = report.get('artifact', {})
    if (artifact.get('id') != row['artifact_id'] or artifact.get('sha256') != row['artifact_sha256']
            or artifact.get('bytes') != row['artifact_bytes'] or artifact.get('original_acceptance_not_yet_verified') is not False
            or report.get('original_acceptance_validation') != 'passed-release-guard-bound-to-original-artifact-and-ZIP'
            or report.get('originals_unchanged_verified') is not True or report.get('volumes_unchanged_verified') is not True):
        raise ValueError('Original accepted artifact/sidecars/volumes proof missing')
    originals = report.get('original_files', {})
    if originals.get(row['archive_name']) != {'bytes': row['archive_bytes'], 'sha256': row['archive_sha256']}:
        raise ValueError('Original inner ZIP binding missing')
    receipt = report.get('transport', {}).get('receipt', {})
    if (receipt.get('name') != row['archive_name'] + '.acceptance.json'
            or not isinstance(receipt.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', receipt['sha256'])
            or originals.get(receipt['name']) != {'bytes': receipt.get('size'), 'sha256': receipt['sha256']}):
        raise ValueError('Original final acceptance bytes were not preserved')
    build = report.get('build', {})
    link = build.get('restricted_zlib', {}).get('final_link_map', {})
    if (build.get('synthetic_space_fixture') is not False or link.get('synthetic_space_shim_present') is not False
            or link.get('required_inflate_symbols_present') is not True
            or link.get('excluded_gzwrite_deflate_crc_archive_symbols_absent') is not True):
        raise ValueError('Baseline build must pass unmodified production symbol scope')
    for label in ('restored_before_launcher', 'restored_after_launcher'):
        evidence = report.get(label, {})
        if (evidence.get('result') != 'passed' or evidence.get('all_file_sha256_checked') is not True
                or evidence.get('no_extra_or_reparse_entries') is not True):
            raise ValueError('Full restored-tree evidence missing')
    if report['restored_before_launcher'] != report['restored_after_launcher']:
        raise ValueError('Restored tree changed across launcher acceptance')
    launcher = report.get('launcher', {})
    cases = launcher.get('scenarios', [])
    if (launcher.get('result') != 'passed' or launcher.get('commit') != pins['commit']
            or launcher.get('archive_extraction_used') is not False or launcher.get('gpu_inference_verified') is not False
            or len(cases) != 2 or {case.get('scenario') for case in cases} != {'clean-host-path', 'contaminated-host-environment'}
            or any(case.get('result') != 'passed' or case.get('returncode') != 0
                   or case.get('audit', {}).get('passed') is not True or case.get('audit', {}).get('errors') != [] for case in cases)):
        raise ValueError('Complete original restored-launcher acceptance missing')
