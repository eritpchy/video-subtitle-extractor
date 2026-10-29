#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Stdlib-only source-ZIP rebuild bootstrap, embedded verbatim in its workflow.

No checkout, original app ZIP, volumes, producer objects, new tool installation,
process enumeration or security-policy modification. Reports have a distinct
source-only scope; they do NOT claim the legacy transport inputs_after proof.
"""
from __future__ import annotations
import base64
import datetime
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import uuid
import unicodedata

sys.dont_write_bytecode = True
import zipfile

APPLICATION_PINS_B64 = 'ewogICJyZXBvc2l0b3J5IjogImVyaXRwY2h5L3ZpZGVvLXN1YnRpdGxlLWV4dHJhY3RvciIsCiAgImNvbW1pdCI6ICJjNDVjMGIwM2QzZWVmNTQ4Y2FhNjliODQ1M2UyYWE4MDM3NjA5OGU5IiwKICAidmVyc2lvbiI6ICIyLjMuMSIsCiAgInJ1bl9pZCI6IDQwNzIyNzc5MDY0LAogICJydW5fYXR0ZW1wdCI6IDIsCiAgInByb2ZpbGVzIjogewogICAgInBhZGRsZS1jdWRhMTEuOCI6IHsKICAgICAgImNhbm9uaWNhbF9hcnRpZmFjdF9uYW1lIjogInJlbGVhc2UtY2FuZGlkYXRlLXdpbmRvd3MteDY0LXBhZGRsZS1jdWRhMTEuOCIsCiAgICAgICJpbm5lcl96aXAiOiAidnNlLTIuMy4xLXdpbmRvd3MteDY0LWN1ZGExMS44LnppcCIsCiAgICAgICJjYW5vbmljYWxfYXJ0aWZhY3RfaWQiOiAxMjg3MzE1NjUxMiwKICAgICAgIm91dGVyX2NvbnRhaW5lcl9ieXRlcyI6IDI5ODU3OTQ5NjMsCiAgICAgICJvdXRlcl9hcGlfc2hhMjU2IjogIjMyNmQyNDM0NDMxOWFjZjczNWE2MjJjNGNkZGZkMDk0ZTRjZDVlMjM2NzMyNTk4ZTVlNTFmMWU3YTUwMmQ5ZmIiLAogICAgICAiaW5uZXJfemlwX2J5dGVzIjogMjk4NTc4OTIzMiwKICAgICAgImlubmVyX3ppcF9zaGEyNTYiOiAiOGM1MTY0ODZlZGQ0MzI3NzRlODg0M2M0NmEzNTUzN2UwNDI3YWNlZTMwMjAzMmY3M2YzZWZlYTEwYjE3NGE3NiIsCiAgICAgICJyZWNlaXB0X2J5dGVzIjogNTA4MSwKICAgICAgInJlY2VpcHRfc2hhMjU2IjogIjIwZWRiNGVjM2Y4MTQ3NWUxMmM3OTQzY2RiYzIxMTAwMWQyNzRlMTZiYjA2ZGMwOGFmNjc5MGVlYmVjODQ2NmUiLAogICAgICAiY2hlY2tzdW1fYnl0ZXMiOiAxMDIsCiAgICAgICJjaGVja3N1bV9zaGEyNTYiOiAiOTIzNmQ4MjU2ODAyNWNlMGUxN2JhMzcxZTc1YmFhZmVlNmEwMDhmZjI3NmQ4YzJhYWFlYTA3MTgyZWZlMWY0ZCIsCiAgICAgICJleHBhbmRlZF9ieXRlcyI6IDUzMTY5MTQ4MTksCiAgICAgICJleHBpcnlfdXRjIjogIjIwMjYtMTAtMzBUMDM6NDE6MDBaIiwKICAgICAgImpvYiI6IDEyMzI0OTQzMjkwNgogICAgfSwKICAgICJwYWRkbGUtY3VkYTEyLjYiOiB7CiAgICAgICJjYW5vbmljYWxfYXJ0aWZhY3RfbmFtZSI6ICJyZWxlYXNlLWNhbmRpZGF0ZS13aW5kb3dzLXg2NC1wYWRkbGUtY3VkYTEyLjYiLAogICAgICAiaW5uZXJfemlwIjogInZzZS0yLjMuMS13aW5kb3dzLXg2NC1jdWRhMTIuNi56aXAiLAogICAgICAiY2Fub25pY2FsX2FydGlmYWN0X2lkIjogMTI4NzI4NjA3ODgsCiAgICAgICJvdXRlcl9jb250YWluZXJfYnl0ZXMiOiAyNjk2NzgxMDY3LAogICAgICAib3V0ZXJfYXBpX3NoYTI1NiI6ICJiNWFiN2NkZWQ5NmRmZTAxNzk5OWE2MjM3MTAxOTMwY2M5M2M4ZThlZGMyYjlhYWQzMzU5NjBmNTQyZDNmNWQ2IiwKICAgICAgImlubmVyX3ppcF9ieXRlcyI6IDI2OTY3NzUzMzYsCiAgICAgICJpbm5lcl96aXBfc2hhMjU2IjogIjhlZjdiMjUzNzVjOTliMTE4M2E2OGFiMDZhYTcyZjk0OTlhNjg2Y2Y3YmVhOTljMjk1OGQyOTcwNjIzYzJlOGUiLAogICAgICAicmVjZWlwdF9ieXRlcyI6IDUwODEsCiAgICAgICJyZWNlaXB0X3NoYTI1NiI6ICJhMmU1YjJjNDIyMDhhOTdmMmE1Y2JiYmQ5ZmE1MDBmNzU0MmE5MTI3OGI4NjllOTQ5NTA1ZDAxMmQyMTg2MjkxIiwKICAgICAgImNoZWNrc3VtX2J5dGVzIjogMTAyLAogICAgICAiY2hlY2tzdW1fc2hhMjU2IjogImE3YzI5MzUwMGVlMjAyMGJjNjJiNTlkNTBlNWVkMzNhNWNkNTc4ZWExMmNhNzdiOTA3Njk0YTlmMWYyMzliYzEiLAogICAgICAiZXhwYW5kZWRfYnl0ZXMiOiA0NDk5MzYyNzM3LAogICAgICAiZXhwaXJ5X3V0YyI6ICIyMDI2LTEwLTMwVDAzOjQxOjAwWiIsCiAgICAgICJqb2IiOiAxMjMyNDk0MzI4NzUKICAgIH0KICB9LAogICJzY2hlbWEiOiAyLAogICJzY29wZSI6ICJleHBsaWNpdC1maW5hbC1jdWRhLWlucHV0cy1idWlsZC1vbmx5IiwKICAiY2FuZGlkYXRlX3NldF9qb2IiOiAxMjMyNjIyNjkxODEsCiAgInByb2plY3Rfbm90aWNlIjogewogICAgInNvdXJjZSI6ICJuYXRpdmUvb2NyL3ZjLXJ1bnRpbWUtTk9USUNFLnR4dCIsCiAgICAibmFtZSI6ICJNaWNyb3NvZnQtVmlzdWFsLUNwcC1ydW50aW1lLU5PVElDRS50eHQiLAogICAgImJ5dGVzIjogMjAwMSwKICAgICJzaGEyNTYiOiAiMDhmOWVhNjg1ODkzNjBmNjJkZjM1YjYyMzJlNGZlNjMwN2IxMWQ0NmU0ZjBmYmIzMjdlMTYwYmZiNmYwZTUwZiIKICB9Cn0K'
APPLICATION_PINS_SHA256 = '2a78c24c73b730233ced1d2cf17502740cb71c2a224240a6a8fac24d9716da4c'
PROFILES = ('paddle-cuda11.8', 'paddle-cuda12.6')
RUNNERS = {'windows-2022': 20348, 'windows-2025': 26100}
RESERVE = 4 * 1024 ** 3
SOURCE_LIMIT = 256 * 1024 ** 2
SAFE_ENV = {'SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','PATH','PATHEXT','NUMBER_OF_PROCESSORS',
    'PROCESSOR_ARCHITECTURE','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','PROGRAMDATA',
    'SYSTEMDRIVE','USERPROFILE','LOCALAPPDATA','APPDATA','USERNAME','USERDOMAIN','OS'}


def need(value, message):
    if not value: raise RuntimeError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def digest(data): return hashlib.sha256(data).hexdigest()


def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream, 'sha256').hexdigest()


def hash_text(value):
    need(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'Invalid SHA256')
    return value


def positive(value, label, maximum=16 * 1024 ** 3):
    need(type(value) is int and 0 < value <= maximum, 'Missing/invalid ' + label)
    return value


def expiry_text(value):
    need(isinstance(value, str) and value.endswith('Z'), 'Explicit UTC expiry required')
    try: result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error: raise RuntimeError('Invalid canonical expiry') from error
    need(result > datetime.datetime.now(datetime.timezone.utc), 'Canonical input expired')
    return value


def validate_input_pins(pin):
    need(isinstance(pin, dict) and set(pin) == {'schema','scope','repository','commit','version','run_id',
         'run_attempt','candidate_set_job','project_notice','profiles'}, 'Exact explicit input schema required')
    need(pin['schema'] == 2 and pin['scope'] == 'explicit-final-cuda-inputs-build-only'
         and pin['repository'] == 'eritpchy/video-subtitle-extractor', 'Wrong explicit input contract')
    need(isinstance(pin['commit'], str) and re.fullmatch('[0-9a-f]{40}', pin['commit']), 'Final source SHA required')
    need(isinstance(pin['version'], str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pin['version']), 'Final version required')
    for key in ('run_id','run_attempt','candidate_set_job'): positive(pin[key], key, 2**63-1)
    notice = pin['project_notice']
    need(isinstance(notice, dict) and set(notice) == {'source','name','bytes','sha256'}
         and notice['source'] == 'native/ocr/vc-runtime-NOTICE.txt'
         and notice['name'] == 'Microsoft-Visual-Cpp-runtime-NOTICE.txt', 'Exact committed project notice required')
    positive(notice['bytes'], 'project notice bytes', 1024**2); hash_text(notice['sha256'])
    need(isinstance(pin['profiles'], dict) and set(pin['profiles']) == set(PROFILES), 'Both final CUDA profiles required')
    artifact_ids = set(); jobs = {pin['candidate_set_job']}
    keys = {'canonical_artifact_id','canonical_artifact_name','outer_container_bytes','outer_api_sha256',
            'inner_zip','inner_zip_bytes','inner_zip_sha256','receipt_bytes','receipt_sha256',
            'checksum_bytes','checksum_sha256','expanded_bytes','expiry_utc','job'}
    for profile, row in pin['profiles'].items():
        need(isinstance(row, dict) and set(row) == keys, 'Exact canonical profile pin fields required')
        positive(row['canonical_artifact_id'], 'canonical artifact ID', 2**63-1); positive(row['job'], 'producer job', 2**63-1)
        need(row['canonical_artifact_id'] not in artifact_ids and row['job'] not in jobs, 'Aliased canonical artifact/job')
        artifact_ids.add(row['canonical_artifact_id']); jobs.add(row['job'])
        need(row['canonical_artifact_name'] == 'release-candidate-windows-x64-' + profile
             and row['inner_zip'] == 'vse-' + pin['version'] + '-windows-x64-' + profile.removeprefix('paddle-') + '.zip',
             'Canonical name/version/profile mismatch')
        for key in ('outer_container_bytes','inner_zip_bytes','expanded_bytes'): positive(row[key], key)
        positive(row['receipt_bytes'], 'receipt bytes', 2*1024**2); positive(row['checksum_bytes'], 'checksum bytes', 4096)
        for key in ('outer_api_sha256','inner_zip_sha256','receipt_sha256','checksum_sha256'): hash_text(row[key])
        expiry_text(row['expiry_utc'])
    return pin


def configured_pin_bytes():
    try: raw = base64.b64decode(APPLICATION_PINS_B64, validate=True)
    except (ValueError, TypeError) as error: raise RuntimeError('Invalid embedded input binding') from error
    need(digest(raw) == hash_text(APPLICATION_PINS_SHA256), 'Embedded input binding SHA mismatch')
    return raw


def expected_pins():
    return validate_input_pins(json_bytes(configured_pin_bytes()))


def binding_identity(pin, profile):
    validate_input_pins(pin); need(profile in PROFILES, 'Unknown bound profile')
    return {'repository': pin['repository'], 'commit': pin['commit'], 'version': pin['version'],
            'run_id': pin['run_id'], 'run_attempt': pin['run_attempt'], 'candidate_set_job': pin['candidate_set_job'],
            'pins_sha256': APPLICATION_PINS_SHA256, 'canonical': pin['profiles'][profile],
            'project_notice': pin['project_notice']}


def verify_project_notice(root, pin):
    expected = pin['project_notice']; path = root / expected['source']
    manifest = read_json(root / 'native/ocr/vc-runtime-manifest.json')
    need(manifest.get('schema') == 2, 'Current no-DOCX manifest required')
    rows = manifest.get('files', [])
    need(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
         and not any(str(row.get('name','')).casefold().endswith('.docx') for row in rows),
         'Active DOCX inputs are forbidden')
    matching = [row for row in rows if row.get('name') == expected['name']]
    need(len(matching) == 1 and matching[0].get('source_file') == 'vc-runtime-NOTICE.txt'
         and matching[0].get('bytes') == expected['bytes'] and matching[0].get('sha256') == expected['sha256'],
         'Project notice differs from explicit input contract')
    need(file_record(path) == {key: expected[key] for key in ('bytes','sha256')}, 'Committed project notice bytes changed')
    review = manifest.get('licensing_review', {})
    need(review.get('status') == 'applicable-terms-not-independently-verified'
         and review.get('final_publication_authorized') is False
         and review.get('fixed_historical_DOCX_download_required') is False,
         'Project attribution must not impersonate verified rights or publication approval')
    return {'scope': 'committed-project-attribution-not-license-agreement', **expected,
            'licensing_review': review, 'network_used': False}


def plain(path, directory=False):
    info = Path(path).lstat()
    need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400
         and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)), 'Non-plain input')
    return Path(path)


def file_record(path):
    path = plain(path)
    return {'bytes': path.stat().st_size, 'sha256': sha(path)}


def json_bytes(data):
    need(len(data) <= 64 * 1024 ** 2, 'Oversized JSON')
    def pairs(rows):
        result = {}
        for name, value in rows:
            need(name not in result, 'Duplicate JSON key'); result[name] = value
        return result
    result = json.loads(data, object_pairs_hook=pairs)
    need(isinstance(result, dict), 'JSON object required')
    return result


def read_json(path): return json_bytes(plain(path).read_bytes())


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())


def load_exact(path, name):
    spec = importlib.util.spec_from_file_location('_restorer_distribution_' + name, plain(path))
    need(spec is not None and spec.loader is not None, 'Exact source loader unavailable')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def space(path, required):
    free = shutil.disk_usage(path).free
    need(free >= required, f'Insufficient space: required {required}, available {free}')
    return {'required_bytes': required, 'available_bytes': free}


def native_server(runner):
    need(os.name == 'nt' and runner in RUNNERS, 'Native Windows Server required')
    value = sys.getwindowsversion()
    need(value.product_type == 3 and value.build == RUNNERS[runner] and ctypes.sizeof(ctypes.c_void_p) == 8,
         'Server identity/x64 mismatch')
    return {'runner': runner, 'build': value.build, 'product_type': value.product_type}


def safe_name(name):
    need(isinstance(name, str) and name and '\\' not in name and '\x00' not in name
         and not name.startswith('/') and str(PurePosixPath(name)) == name, 'Unsafe archive name')
    for part in name.split('/'):
        base = part.split('.', 1)[0].rstrip(' ').upper()
        need(part not in ('', '.', '..') and part[-1] not in '. '
             and not any(ord(c) < 32 or c in ':*?"<>|' for c in part)
             and not re.fullmatch(r'(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[0-9¹²³]|LPT[0-9¹²³])', base),
             'Unsafe archive component')
    return name


def unpack(archive, destination, *, limit, expected=None):
    """Bounded plain files, no overwrite/links; exact expected bytes when supplied."""
    plain(archive); destination = Path(destination)
    with zipfile.ZipFile(archive) as zipped:
        rows = zipped.infolist(); names = set(); total = 0
        need(0 < len(rows) <= 10000, 'Archive entry bound exceeded')
        for row in rows:
            name = safe_name(row.orig_filename)
            need(name == row.filename and not row.is_dir() and not row.flag_bits & 1
                 and not row.external_attr & 0x400
                 and row.compress_type in (0, 8) and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG),
                 'Unsupported archive member')
            key = unicodedata.normalize('NFC', name).casefold()
            need(key not in names and not any(key.startswith(old + '/') or old.startswith(key + '/') for old in names),
                 'Duplicate/case alias or file-parent conflict'); names.add(key)
            total += row.file_size; need(total <= limit, 'Expanded archive bound exceeded')
        if expected is not None:
            need({row.filename for row in rows} == set(expected), 'Archive member set differs from fixed identity')
        space(destination.parent, total + RESERVE)
        destination.mkdir(parents=False, exist_ok=False)
        for row in rows:
            path = destination / row.filename; path.parent.mkdir(parents=True, exist_ok=True)
            copied = 0
            with zipped.open(row) as source, path.open('xb') as output:
                while chunk := source.read(1024 ** 2):
                    copied += len(chunk); need(copied <= row.file_size, 'Member exceeded declared bound'); output.write(chunk)
            need(copied == row.file_size, 'Truncated archive member')
            if expected is not None: need(file_record(path) == expected[row.filename], 'Extracted input SHA mismatch')


def clean_environment():
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    return env


def initialize_msvc(report):
    """Installed Microsoft 14.44 only. Child VsDevCmd, no third-party setup action.

    Quoting follows the already-reviewed exact8f windows_toolchain.py contract;
    no PowerShell policy switch, installer, system env change or process scan.
    """
    need(os.name == 'nt', 'MSVC initialization is Windows-only')
    env = clean_environment()
    lower = {k.upper(): v for k, v in env.items()}
    vswhere = Path(lower['PROGRAMFILES(X86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    result = subprocess.run([str(vswhere), '-all', '-products', '*', '-requires',
        'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json', '-utf8'],
        env=env, check=True, capture_output=True, timeout=60)
    instances = json.loads(result.stdout.decode('utf-8-sig')); candidates = []
    for instance in instances:
        installation = Path(instance['installationPath'])
        for directory in (installation / 'VC/Tools/MSVC').glob('14.44.*'):
            if re.fullmatch(r'14\.44\.[0-9]+', directory.name):
                binary = directory / 'bin/Hostx64/x64'
                if all((binary / name).is_file() for name in ('cl.exe','link.exe','dumpbin.exe')):
                    candidates.append((tuple(map(int, directory.name.split('.'))), installation, directory))
    need(candidates, 'Reviewed installed MSVC14.44 unavailable; no automatic installation')
    _, installation, directory = max(candidates, key=lambda row: row[0])
    script = installation / 'Common7/Tools/VsDevCmd.bat'
    buffer = ctypes.create_unicode_buffer(32768)
    count = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    need(0 < count < len(buffer), 'GetSystemDirectoryW failed')
    command_shell = Path(buffer.value) / 'cmd.exe'
    for path in (str(command_shell), str(script)):
        need(not any(c in path for c in '\r\n"%!^<>|'), 'Unsafe vendor batch path')
    marker = 'VSE_TOOLCHAIN_ENV_' + uuid.uuid4().hex
    command = (f'"{command_shell}" /d /q /u /s /v:off /c "call "{script}" '
        f'-no_logo -arch=x64 -host_arch=x64 -vcvars_ver={directory.name} && echo {marker}&& set"')
    result = subprocess.run(command, executable=str(command_shell), shell=False, env=env,
                            capture_output=True, timeout=120)
    delimiter = marker.encode('utf-16-le')
    # Never persist the environment suffix, nor secret-containing parent env.
    need(result.returncode == 0 and result.stdout.count(delimiter) == 1, 'VsDevCmd failed or ambiguous environment')
    prefix, _, suffix = result.stdout.partition(delimiter)
    active = {}
    for line in suffix.decode('utf-16-le').splitlines():
        if '=' in line and not line.startswith('='):
            key, value = line.split('=', 1); active[key.upper()] = value
    need(active.get('VSCMD_ARG_TGT_ARCH') == active.get('VSCMD_ARG_HOST_ARCH') == 'x64'
         and active.get('VCTOOLSVERSION', '').rstrip('\\/') == directory.name, 'Wrong initialized toolset')
    for name in ('cl.exe', 'link.exe', 'dumpbin.exe'):
        actual = shutil.which(name, path=active.get('PATH', ''))
        need(actual and Path(actual).resolve() == (directory / 'bin/Hostx64/x64' / name).resolve(), 'Toolset path mismatch')
    for key in ('INCLUDE','LIB','LIBPATH','WINDOWSSDKVERSION'):
        need(active.get(key), 'Missing native toolchain environment')
    for key in ('CL','_CL_','LINK','_LINK_'): active.pop(key, None)
    active.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
    report['toolchain_initialization'] = {'scope': 'installed-14.44-child-environment-only',
        'toolset': directory.name, 'vswhere': file_record(vswhere), 'vendor_script': file_record(script),
        'command': command, 'environment_keys': sorted(active), 'runtime_linkage': '/MT (verified by actual compiler record)',
        'stdout_prefix_sha256': digest(prefix), 'stderr_sha256': digest(result.stderr),
        'compiler': file_record(directory / 'bin/Hostx64/x64/cl.exe')}
    return active


def validate_compile(record, *, expected_exe=None):
    need(record.get('synthetic_space_fixture') is False and record.get('synthetic_translation_units') == [], 'Synthetic build forbidden')
    command = record.get('command', [])
    need(isinstance(command, list) and {'/MT','/W4','/WX','/GS','/guard:cf','/DEPENDENTLOADFLAG:0x800'} <= set(command)
         and not any(value.upper() in ('/MD','/MDD','/MTD','/GL') for value in command), 'Compiler/linker policy missing')
    need(record.get('exe_bytes', 0) > 0, 'EXE bytes missing'); hash_text(record.get('exe_sha256'))
    if expected_exe is not None: need(record['exe_sha256'] == expected_exe, 'Different actual EXE')
    pe = record.get('pe_dependency_policy', {})
    need(pe.get('machine') == 0x8664 and pe.get('optional_header_magic') == 0x20b
         and pe.get('dependent_load_flags') == 0x800 and pe.get('source') == 'PE binary LoadConfig, not dumpbin text', 'PE policy missing')
    imports = record.get('imports', [])
    need(isinstance(imports, list) and imports and set(imports) <= {'KERNEL32.DLL','ADVAPI32.DLL','BCRYPT.DLL','USER32.DLL'}, 'Static CRT import closure missing')
    zlib = record.get('restricted_zlib', {}); link = zlib.get('final_link_map', {})
    need(link.get('synthetic_space_shim_present') is False and link.get('required_inflate_symbols_present') is True
         and link.get('excluded_gzwrite_deflate_crc_archive_symbols_absent') is True
         and zlib.get('excluded_gzwrite_deflate_crc_archive_symbols_absent') is True, 'Raw-inflate link closure missing')
    hash_text(link.get('sha256')); hash_text(zlib.get('pin_sha256'))
    need(link.get('bytes', 0) > 0, 'Link map evidence missing')
    objects = zlib.get('compiled_objects', [])
    need(len(objects) == 5 and {row.get('source') for row in objects} == {'inflate.c','inftrees.c','inffast.c','adler32.c','zutil.c'}, 'Wrong inflater object closure')
    for row in objects:
        hash_text(row.get('object_sha256'))
        need(row.get('defined_symbols') and {'/MT','/W4','/WX','/DZ_SOLO','/DNO_GZIP'} <= set(row.get('command', [])), 'Object gate missing')
    tools = record.get('toolchain', {}).get('tools', {})
    need(set(tools) == {'cl','link','dumpbin'}, 'Actual tool identities missing')
    for row in tools.values(): hash_text(row.get('sha256')); need(row.get('bytes', 0) > 0 and row.get('path'), 'Tool evidence missing')
    for field in ('actual_headers','actual_libraries','actual_sdk_headers'):
        rows = record.get('toolchain', {}).get(field, [])
        need(rows, 'Actual SDK/header/library provenance missing')
        for row in rows: hash_text(row.get('sha256')); need(row.get('bytes', 0) > 0 and row.get('path'), 'Missing input identity')


def validate_context(context):
    pin = expected_pins(); profile = context.get('profile')
    need(context.get('schema') == 1 and profile in PROFILES
         and context.get('source_commit') == pin['commit'] and context.get('source_version') == pin['version']
         and context.get('application_identity') == binding_identity(pin, profile), 'Wrong producer source context')
    row = pin['profiles'][profile]
    for name, expected in (('archive_bytes', row['inner_zip_bytes']), ('expanded_bytes', row['expanded_bytes'])):
        if name in context: need(type(context[name]) is int and context[name] == expected, 'Conflicting producer size binding')
    if 'inventory_entries' in context: positive(context['inventory_entries'], 'inventory entries', 1000000)
    for name in ('source_zip_sha256','original_exe_sha256','handoff_sha256','embedded_header_sha256',
                 'generator_identity_sha256','source_closure_sha256','source_manifest_sha256','original_build_record_sha256'):
        hash_text(context.get(name))
    need(type(context.get('source_zip_bytes')) is int and 0 < context['source_zip_bytes'] <= SOURCE_LIMIT, 'Source ZIP size bound')


def verify_source_inputs(root, context):
    validate_context(context)
    helper = load_exact(root / 'native/windows-restorer/source_bundle.py', 'source_bundle')
    manifest = helper.verify_extracted_sources(root)
    original_path = root / 'evidence/original-build-record.json'
    original = read_json(original_path)
    need(sha(root / 'source-manifest.json') == context['source_manifest_sha256']
         and sha(original_path) == context['original_build_record_sha256'], 'Source manifest/build-record identity mismatch')
    need(manifest['original_exe_sha256'] == original['exe_sha256'] == context['original_exe_sha256']
         and manifest.get('git') == original.get('git'), 'Original EXE/source Git identity mismatch')
    need(original.get('git', {}).get('commit') == expected_pins()['commit'] and original['git'].get('dirty') is False
         and original.get('git_after_build') == original['git'], 'Unclean or foreign original source')
    need(sha(root / 'generated/bundle_metadata.hpp') == original['embedded_header_sha256'] == context['embedded_header_sha256'], 'Fixed-header identity mismatch')
    need(digest(canonical(original['generator_dependencies'])) == context['generator_identity_sha256']
         and digest(canonical(original['sources'])) == context['source_closure_sha256'], 'Generator/source closure identity mismatch')
    for name, expected in original['sources'].items():
        safe_name(name); need(sha(root / name) == expected, 'Used source differs from producer')
    validate_compile(original, expected_exe=context['original_exe_sha256'])
    pin = expected_pins(); verify_project_notice(root, pin)
    notice = pin['project_notice']
    required_notices = {'Microsoft-Visual-Cpp-runtime-NOTICE.txt':'native/ocr/vc-runtime-NOTICE.txt',
        'Apache-2.0.txt':'LICENSE', 'THIRD_PARTY_NOTICES.txt':'native/windows-restorer/THIRD_PARTY_NOTICES.txt',
        'zlib-LICENSE.txt':'native/windows-restorer/third_party/zlib-1.3.2/LICENSE'}
    notice_root = plain(root / 'notices', True)
    need({path.name for path in notice_root.iterdir()} == set(required_notices)
         and set(original.get('notices', {})) == set(required_notices), 'Complete four-notice closure required')
    for name, source in required_notices.items():
        need(sha(notice_root / name) == original['notices'][name] == sha(root / source), 'Delivered notice differs from exact source')
    need(file_record(root / 'notices' / notice['name']) == {key:notice[key] for key in ('bytes','sha256')}, 'Delivered project notice changed')
    transport_path = root / 'evidence/transport-manifest.json'
    need(sha(transport_path) == original.get('archive_transport_manifest_sha256'), 'Original transport metadata hash mismatch')
    document = read_json(transport_path); transport = document['archive']
    row = pin['profiles'][context['profile']]
    need(transport.get('name') == row['inner_zip'] and transport.get('sha256') == row['inner_zip_sha256']
         and transport.get('size') == row['inner_zip_bytes'] and transport.get('uncompressed_bytes') == row['expanded_bytes']
         and transport.get('identity', {}).get('commit') == pin['commit']
         and transport.get('identity', {}).get('version') == pin['version']
         and transport.get('identity', {}).get('build_profile') == context['profile'], 'Source transport differs from final application binding')
    for label in ('receipt', 'checksum'):
        need(document.get(label, {}).get('name') == row['inner_zip'] + ('.acceptance.json' if label == 'receipt' else '.sha256')
             and document.get(label, {}).get('sha256') == row[label + '_sha256']
             and document.get(label, {}).get('size') == row[label + '_bytes'], 'Source transport sidecar binding changed')
    return {'result': 'passed', 'source_manifest_sha256': sha(root / 'source-manifest.json'),
            'original_build_record_sha256': sha(original_path), 'embedded_header_sha256': context['embedded_header_sha256'],
            'source_closure_sha256': context['source_closure_sha256'], 'generator_identity_sha256': context['generator_identity_sha256']}


def validate_source_report(report, context, artifact_root=None):
    validate_context(context)
    need(report.get('schema') == 1 and report.get('scope') == 'source-zip-only-msvc-rebuild-no-repository-or-application-archive'
         and report.get('result') == 'passed' and report.get('producer_context') == context
         and report.get('repository_checkout_used') is False and report.get('application_archive_or_volumes_used') is False
         and report.get('post_run_errors') == [] and report.get('gpu_inference_verified') is False,
         'Incomplete source-only evidence or invalid scope')
    need(report.get('native_os') == {'runner':'windows-2025','build':26100,'product_type':3}, 'Wrong independent rebuild Server')
    expected_source = {'result':'passed', **{key:context[key] for key in ('source_manifest_sha256',
        'original_build_record_sha256','embedded_header_sha256','source_closure_sha256','generator_identity_sha256')}}
    need(report.get('source_before') == expected_source, 'Source snapshot not bound to producer context')
    need(report.get('source_zip_before') == {'bytes':context['source_zip_bytes'],'sha256':context['source_zip_sha256']}, 'Source ZIP initial identity mismatch')
    need('inputs_after' not in report, 'Source-only report must not impersonate legacy transport input proof')
    need(report.get('source_before') == report.get('source_after') and report.get('source_after', {}).get('result') == 'passed', 'Source changed during rebuild')
    need(report.get('source_zip_after') == {'bytes': context['source_zip_bytes'], 'sha256': context['source_zip_sha256']}, 'Source ZIP changed')
    rebuilt = report.get('rebuild_record', {}); validate_compile(rebuilt)
    need(rebuilt.get('scope') == 'fixed-header-source-rebuild-not-client-certification'
         and rebuilt.get('original_exe_sha256') == context['original_exe_sha256']
         and rebuilt.get('client_restore_verified') is False and rebuilt.get('gpu_inference_verified') is False,
         'Rebuild record scope/identity mismatch')
    need(report.get('rebuilt_exe') == {'bytes': rebuilt['exe_bytes'], 'sha256': rebuilt['exe_sha256']}, 'Actual rebuilt EXE differs')
    need(type(rebuilt.get('bit_identical_to_original')) is bool
         and rebuilt['bit_identical_to_original'] == (rebuilt['exe_sha256'] == context['original_exe_sha256']), 'False bit-identity claim')
    raw = report.get('raw_artifacts', {})
    required = {'rebuild.log','rebuilt/compiler.log','rebuilt/link-map.txt','rebuilt/rebuild-record.json','rebuilt/compile-command-record.json'}
    need(set(raw) == required, 'Missing actual raw rebuild evidence')
    for name, identity in raw.items():
        hash_text(identity.get('sha256')); need(type(identity.get('bytes')) is int and identity['bytes'] > 0, 'Empty raw rebuild evidence')
        if artifact_root is not None:
            need(file_record(Path(artifact_root)/name) == identity, 'Raw rebuild artifact changed or missing')
    need(raw['rebuilt/link-map.txt'] == {key:rebuilt['restricted_zlib']['final_link_map'][key] for key in ('bytes','sha256')}, 'Actual raw link map differs')
    if artifact_root is not None:
        need(read_json(Path(artifact_root)/'rebuilt/rebuild-record.json') == rebuilt, 'Raw rebuild record differs')
        need((Path(artifact_root)/'rebuilt/compiler.log').read_text(encoding='utf-8') == rebuilt['toolchain']['compiler_bv_and_link_log'], 'Raw compiler log differs')
    initialized = report.get('toolchain_initialization', {})
    need(initialized.get('scope') == 'installed-14.44-child-environment-only'
         and re.fullmatch(r'14\.44\.[0-9]+', initialized.get('toolset',''))
         and initialized.get('compiler') == {key:rebuilt['toolchain']['tools']['cl'][key] for key in ('bytes','sha256')},
         'Actual rebuilt toolchain differs from selected installed MSVC 14.44')
    argv = report.get('rebuild_command', [])
    need(all(value in argv for value in ('-I','-S','-B','--source-root','--output','--acknowledge-redistribution-license')), 'Nonisolated rebuild command')


def main():
    context = json_bytes(os.environ['VSE_SOURCE_CONTEXT'].encode()); validate_context(context)
    need(os.environ.get('VSE_ACKNOWLEDGE_VC_LICENSE') == 'true', 'Explicit distributor rights acknowledgment required')
    runner = os.environ['VSE_RUNNER']; os_record = native_server(runner)
    archive = Path(os.environ['VSE_SOURCE_ZIP']).absolute()
    expected = {'bytes': context['source_zip_bytes'], 'sha256': context['source_zip_sha256']}
    need(file_record(archive) == expected, 'Producer source ZIP byte identity mismatch')
    work = Path(os.environ['VSE_REBUILD_WORK']).absolute(); work.mkdir(parents=False, exist_ok=False)
    report = {'schema': 1, 'scope': 'source-zip-only-msvc-rebuild-no-repository-or-application-archive',
        'result': 'failed', 'producer_context': context, 'native_os': os_record,
        'repository_checkout_used': False, 'application_archive_or_volumes_used': False,
        'gpu_inference_verified': False, 'source_zip_before': expected}
    failure = None; extracted = False; source = work / 'source'
    try:
        report['budget'] = space(work, SOURCE_LIMIT + RESERVE)
        unpack(archive, source, limit=SOURCE_LIMIT); extracted = True
        report['source_before'] = verify_source_inputs(source, context)
        env = initialize_msvc(report)
        command = [sys.executable, '-I', '-S', '-B', str(source / 'native/windows-restorer/rebuild.py'),
            '--source-root', str(source), '--output', str(work / 'rebuilt'), '--acknowledge-redistribution-license']
        report['rebuild_command'] = command
        with (work / 'rebuild.log').open('xb') as log:
            subprocess.run(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
        report['rebuild_record'] = read_json(work / 'rebuilt/rebuild-record.json')
        report['rebuilt_exe'] = file_record(work / 'rebuilt/restore.exe')
        report['raw_artifacts'] = {name:file_record(work/name) for name in ('rebuild.log','rebuilt/compiler.log',
            'rebuilt/link-map.txt','rebuilt/rebuild-record.json','rebuilt/compile-command-record.json')}
    except Exception as error:
        failure = error; report['error'] = f'{type(error).__name__}: {error}'
    finally:
        errors = []
        try:
            report['source_zip_after'] = file_record(archive); need(report['source_zip_after'] == expected, 'Source ZIP changed')
        except Exception as error: errors.append({'check': 'source-zip-after', 'error': str(error)})
        if extracted:
            try: report['source_after'] = verify_source_inputs(source, context)
            except Exception as error: errors.append({'check': 'source-tree-after', 'error': str(error)})
        report['post_run_errors'] = errors
        if failure is None and not errors:
            report['result'] = 'passed'
            try: validate_source_report(report, context, work)
            except Exception as error: report['result'] = 'failed'; report['error'] = str(error)
        write_json(work / 'source-only-report.json', report)
        output = os.environ.get('GITHUB_OUTPUT')
        if output:
            with open(output, 'a', encoding='utf-8') as stream:
                stream.write('report_sha256=' + sha(work / 'source-only-report.json') + '\n')
    need(report['result'] == 'passed', 'Source-only rebuild failed; raw evidence retained')


if __name__ == '__main__': main()
