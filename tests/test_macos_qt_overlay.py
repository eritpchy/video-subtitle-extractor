"""Portable contracts only: synthetic binaries are not macOS AX acceptance."""
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import struct
import threading
import urllib.request
import uuid

import pytest

from scripts import macos_qt_overlay as module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')



SYNTHETIC_MACOS_PURELIB = '/Users/synthetic/vse-venv/lib/python3.12/site-packages'


def macos_receipt(receipt, path):
    """Model a Mac-produced receipt independently of this test host's paths.

    These bytes describe a synthetic Mac origin, not the Windows/Linux fixture
    directory. verify_overlay deliberately checks relocated files separately.
    Do not patch or weaken the production POSIX identity validator.
    """
    receipt['purelib'] = SYNTHETIC_MACOS_PURELIB
    write_json(path, receipt)
    return receipt


def apply_synthetic_overlay(purelib, receipt_path, resource, *, context):
    receipt = module._apply(purelib, receipt_path, resource, context=context)
    return macos_receipt(receipt, receipt_path)


def macho():
    def string(kind, value):
        payload = value.encode() + b'\0'
        size = (12 + len(payload) + 7) & ~7
        return struct.pack('<3I', kind, size, 12) + payload + b'\0' * (size - 12 - len(payload))
    commands = [struct.pack('<6I', 0x32, 24, 1, 0x0E0400, 0x1A0000, 0),
                struct.pack('<2I', 0x1B, 24) + uuid.UUID(int=1).bytes,
                string(0x8000001C, '@loader_path/../../lib'),
                string(0xC, '@rpath/QtCore.framework/Versions/A/QtCore'),
                string(0xC, '@rpath/QtGui.framework/Versions/A/QtGui')]
    return struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 8, len(commands), sum(map(len, commands)), 0, 0) + b''.join(commands)


@pytest.fixture
def resource(tmp_path):
    root = tmp_path / 'resource-root'
    root.mkdir()
    binary = macho()
    (root / 'libqcocoa.dylib').write_bytes(binary)
    (root / 'LICENSES').mkdir()
    for name in ('LGPL-3.0-only.txt', 'GPL-3.0-only.txt', 'BSD-3-Clause.txt'):
        (root / 'LICENSES' / name).write_text('Synthetic license fixture\n' * 10)
    pins = {'patched_sources': {}}
    write_json(root / 'candidate.json', {'candidate_sha256': digest(binary),
               'diagnostic_only': True, 'production_integration_allowed': False, 'pins': pins})
    source = {'name': 'source.tar.xz', 'sha256': digest(b'source'), 'bytes': 6}
    write_json(root / 'resource-manifest.json', {'schema': 1, 'asset_kind': 'vse-qt-cocoa-overlay-resource',
               'qt_version': '6.12.0', 'arch': 'arm64',
               'source_asset': source, 'files': module.archives.inventory(root)})
    archive = tmp_path / 'resource.zip'
    module.archives.write_archive(root, archive)
    policy = {'schema': 1, 'qt_version': '6.12.0', 'architecture': 'arm64',
              'minimum_macos_version': '26.0', 'production_integration_allowed': False,
              'candidate_manifest_sha256': module.sha256(root / 'candidate.json'), 'abi_files': {},
              'resource_manifest_sha256': module.sha256(root / 'resource-manifest.json'),
              'source_url': 'https://github.com/owner/project/releases/download/v2.3.1/source.tar.xz',
              'upstream_pins': pins, 'original_cocoa_sha256': digest(b'original'),
              'official_frameworks': {'QtCore': digest(b'core'), 'QtGui': digest(b'gui')},
              'plugin': {'sha256': digest(binary), 'bytes': len(binary), 'uuid': str(uuid.UUID(int=1))},
              'source_resource': source, 'resource': {'name': archive.name, 'sha256': module.sha256(archive),
              'bytes': archive.stat().st_size, 'root': root.name,
              'url': 'https://github.com/owner/project/releases/download/v2.3.1/resource.zip'}}
    return module.prepare_resource(archive, tmp_path / 'extracted', policy=policy)


@pytest.fixture
def purelib(tmp_path, resource):
    purelib = tmp_path / 'venv/lib/python3.12/site-packages'
    plugin = purelib / module.PLUGIN
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b'original')
    for name, data in [('QtCore', b'core'), ('QtGui', b'gui')]:
        path = purelib / f'PySide6/Qt/lib/{name}.framework/Versions/A/{name}'
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
    for name in ['pyside6_essentials', 'shiboken6']:
        metadata = purelib / f'{name}-6.12.0.dist-info'
        metadata.mkdir()
        (metadata / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 6.12.0\n')
        (metadata / 'RECORD').write_text('original wheel RECORD\n')
    return purelib


def test_real_zip_hash_inventory_and_native_identity(resource):
    result = module._validate_resource(resource)
    assert result['qt_version'] == '6.12.0'
    assert resource.policy['production_integration_allowed'] is False


@pytest.mark.parametrize('mutation', ['plugin', 'extra', 'candidate', 'license', 'archive'])
def test_resource_mutation_rejected(resource, mutation):
    path = {'plugin': resource.root / 'libqcocoa.dylib', 'extra': resource.root / 'unexpected',
            'candidate': resource.root / 'candidate.json', 'license': resource.root / 'LICENSES/LGPL-3.0-only.txt',
            'archive': resource.archive}[mutation]
    path.write_bytes(b'modified')
    with pytest.raises(RuntimeError):
        module._validate_resource(resource)


def test_single_plugin_atomic_replacement_retains_records_and_idempotence(resource, purelib):
    before = module.archives.inventory(purelib)
    receipt_path = purelib / module.RECEIPT
    receipt = apply_synthetic_overlay(purelib, receipt_path, resource, context='explicit-user-venv')
    after = module.archives.inventory(purelib)
    assert [k for k in before if before[k] != after[k]] == [module.PLUGIN.as_posix()]
    assert set(after) - set(before) == {module.RECEIPT}
    assert receipt['external_ax_client_tested'] is False
    assert receipt['production_integration_allowed'] is False
    assert apply_synthetic_overlay(purelib, receipt_path, resource, context='explicit-user-venv') == receipt
    module.verify_overlay(purelib, receipt, policy=resource.policy)


def test_failed_receipt_write_rolls_back_only_owned_plugin(resource, purelib, monkeypatch):
    before = module.archives.inventory(purelib)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(module, '_atomic_json', fail)
    with pytest.raises(OSError, match='disk full'):
        apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    assert module.archives.inventory(purelib) == before


def test_unknown_plugin_or_framework_is_never_replaced(resource, purelib):
    plugin = purelib / module.PLUGIN
    plugin.write_bytes(b'unknown modification')
    with pytest.raises(RuntimeError, match='unknown'):
        apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    assert plugin.read_bytes() == b'unknown modification'


def test_plugin_symlink_cannot_escape_venv(resource, purelib, tmp_path):
    plugin = purelib / module.PLUGIN
    outside = tmp_path / 'system-plugin'
    outside.write_bytes(plugin.read_bytes())
    plugin.unlink()
    try:
        plugin.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not permitted on this test host")
    with pytest.raises(RuntimeError, match='regular file'):
        module.inspect_target(purelib, policy=resource.policy)
    assert outside.read_bytes() == b'original'


def test_already_patched_without_receipt_is_not_adopted(resource, purelib):
    (purelib / module.PLUGIN).write_bytes((resource.root / 'libqcocoa.dylib').read_bytes())
    with pytest.raises(RuntimeError, match='not adopted'):
        apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')


def test_version_mismatch_and_duplicate_metadata_fail(resource, purelib):
    metadata = purelib / 'shiboken6-6.12.0.dist-info/METADATA'
    metadata.write_text('Name: shiboken6\nVersion: 6.13.0\n')
    with pytest.raises(RuntimeError, match='exact reviewed'):
        module.inspect_target(purelib, policy=resource.policy)


def test_production_admission_remains_blocked(resource, purelib, monkeypatch):
    monkeypatch.setattr(module, '_host', lambda policy: None)
    before = module.archives.inventory(purelib)
    with pytest.raises(RuntimeError, match='admission'):
        module.apply_to_venv(resource)
    assert module.archives.inventory(purelib) == before


def test_system_python_and_shared_site_venvs_are_refused(tmp_path, monkeypatch):
    prefix = tmp_path / 'prefix'
    prefix.mkdir()
    monkeypatch.setattr(module.sys, 'prefix', str(prefix))
    monkeypatch.setattr(module.sys, 'base_prefix', str(prefix))
    with pytest.raises(RuntimeError, match='own venv'):
        module.inspect_current_venv()
    monkeypatch.setattr(module.sys, 'base_prefix', str(tmp_path))
    (prefix / 'pyvenv.cfg').write_text('include-system-site-packages = true\n')
    with pytest.raises(RuntimeError, match='disable system-site'):
        module.inspect_current_venv()


@pytest.mark.parametrize('url', ['http://github.com/a', 'https://example.com/a',
                               'https://github.com@evil.example/a', 'https://user:secret@github.com/a',
                               'file:///tmp/overlay', 'https://github.com:443/a'])
def test_unpinned_download_origin_is_refused(url):
    with pytest.raises(RuntimeError):
        module._secure_url(url)


@pytest.mark.parametrize('wrong_hash,overlong', [(False, False), (True, False), (False, True)])
def test_http_transfer_actual_bytes_checked_before_install(resource, tmp_path, monkeypatch, wrong_hash, overlong):
    data = resource.archive.read_bytes() + (b'extra' if overlong else b'')
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    # Production pins and redirect validation remain HTTPS-only. The test
    # replaces only the transport with a real bounded local HTTP transfer.
    local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    class Transport:
        def open(self, url, timeout):
            return local_opener.open(f'http://127.0.0.1:{server.server_port}/asset', timeout=timeout)
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: Transport())
    policy = copy.deepcopy(resource.policy)
    policy['production_integration_allowed'] = True
    if wrong_hash:
        policy['resource']['sha256'] = '0' * 64
    destination = tmp_path / 'download/resource.zip'
    try:
        if wrong_hash or overlong:
            with pytest.raises(RuntimeError):
                module.fetch_archive(destination, policy=policy)
            assert not destination.exists()
        else:
            module.fetch_archive(destination, policy=policy)
            assert destination.read_bytes() == resource.archive.read_bytes()
        assert not list(destination.parent.glob('.vse-qt-download-*'))
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_old_macos_and_rosetta_are_not_claimed_supported(resource, monkeypatch):
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(module.platform, 'mac_ver', lambda: ('15.7.9', (), ''))
    with pytest.raises(RuntimeError, match='26.0'):
        module._host(resource.policy)
    monkeypatch.setattr(module.platform, 'mac_ver', lambda: ('26.6.2', (), ''))
    module._host(resource.policy)
    monkeypatch.setattr(module.platform, 'machine', lambda: 'x86_64')
    with pytest.raises(RuntimeError, match='Rosetta'):
        module._host(resource.policy)


def test_full_corresponding_source_is_required_before_notices(resource, tmp_path):
    source = tmp_path / 'source.tar.xz'
    source.write_bytes(b'source')
    stage = tmp_path / 'stage'; stage.mkdir()
    report = module.stage_notices(stage, resource, source)
    notice = stage / report['directory']
    assert (notice / source.name).read_bytes() == b'source'
    assert not (notice / 'libqcocoa.dylib').exists()
    assert (notice / resource.policy['resource']['name']).read_bytes() == resource.archive.read_bytes()
    assert (notice / 'original-resource-manifest.json').is_file()
    assert module.read_json(notice / 'candidate.json')['production_integration_allowed'] is False
    with pytest.raises(RuntimeError, match='fresh'):
        module.stage_notices(stage, resource, source)


def test_notice_source_hash_mismatch_never_changes_stage(resource, tmp_path):
    source = tmp_path / 'source.tar.xz'; source.write_bytes(b'wrong source')
    stage = tmp_path / 'stage'; stage.mkdir()
    with pytest.raises(RuntimeError, match='identity'):
        module.stage_notices(stage, resource, source)
    assert list(stage.iterdir()) == []


def test_stage_api_rejects_desktop_even_with_explicit_candidate_flag(resource, purelib, monkeypatch):
    monkeypatch.setattr(module, '_host', lambda policy: None)
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    with pytest.raises(RuntimeError, match='hosted macOS CI'):
        module.apply_to_stage(purelib, purelib / 'python', resource, candidate_testing=True)


def test_false_candidate_scope_never_downloads_public_asset(resource, tmp_path):
    with pytest.raises(RuntimeError, match='admission'):
        module.fetch_archive(tmp_path / 'out.zip', policy=resource.policy)
    assert not (tmp_path / 'out.zip').exists()


def test_patched_receipt_detects_later_record_modification(resource, purelib):
    receipt = apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    next(purelib.glob('*.dist-info/RECORD')).write_text('modified RECORD')
    with pytest.raises(RuntimeError, match='receipt'):
        module.verify_overlay(purelib, receipt, policy=resource.policy)


def test_unrelated_abi_library_change_is_rejected(resource, purelib):
    policy = copy.deepcopy(resource.policy)
    binding = purelib / 'PySide6/QtCore.abi3.so'; binding.write_bytes(b'changed binding')
    policy['abi_files'] = {'PySide6/QtCore.abi3.so': digest(b'official binding')}
    with pytest.raises(RuntimeError, match='identity'):
        module.inspect_target(purelib, policy=policy)


@pytest.mark.parametrize('case', ['valid', 'sealed', 'foreign-purelib'])
def test_stage_entry_is_isolated_and_leaves_pruning_to_packager(resource, purelib, tmp_path, monkeypatch, case):
    monkeypatch.setattr(module, '_host', lambda _: None)
    for key, value in [('GITHUB_ACTIONS', 'true'), ('RUNNER_ENVIRONMENT', 'github-hosted'), ('RUNNER_OS', 'macOS')]:
        monkeypatch.setenv(key, value)
    stage = tmp_path / 'staging'
    packages = stage / 'runtime/lib/python3.12/site-packages'
    shutil.copytree(purelib, packages)
    python = stage / 'runtime/bin/python'; python.parent.mkdir(); python.write_text('synthetic interpreter')
    prune = stage / 'runtime/inference-prune.json'; prune.write_text('existing pruning inventory')
    if case == 'sealed': (stage / 'bundle-manifest.json').write_text('{}')
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **k: json.dumps(str(purelib if case == 'foreign-purelib' else packages)))
    before = module.archives.inventory(stage)
    if case != 'valid':
        with pytest.raises(RuntimeError):
            module.apply_to_stage(stage, python, resource, candidate_testing=True)
        assert module.archives.inventory(stage) == before
    else:
        receipt = module.apply_to_stage(stage, python, resource, candidate_testing=True)
        macos_receipt(receipt, stage / 'runtime' / module.RECEIPT)
        assert receipt['context'] == 'candidate-package-stage'
        assert (stage / 'runtime' / module.RECEIPT).is_file()
        assert prune.read_text() == 'existing pruning inventory'
        module.verify_overlay(packages, receipt, policy=resource.policy)


def test_venv_license_notices_survive_resource_cleanup_and_reject_tamper(resource, tmp_path):
    prefix = tmp_path / 'own-venv'; prefix.mkdir()
    notice = module._venv_notices(prefix, resource)
    assert (notice / 'LICENSES/LGPL-3.0-only.txt').is_file()
    assert (notice / 'CORRESPONDING-SOURCE.txt').is_file()
    assert not (notice / 'libqcocoa.dylib').exists()
    assert module._venv_notices(prefix, resource) == notice
    (notice / 'LICENSES/LGPL-3.0-only.txt').unlink()
    with pytest.raises(RuntimeError, match='notices differ'):
        module._venv_notices(prefix, resource)


def test_source_notice_hash_uses_the_same_utf8_bytes_as_disk_on_crlf_hosts(resource, tmp_path, monkeypatch):
    original = Path.write_text
    def windows_text_newlines(path, data, *args, **kwargs):
        if path.name == 'CORRESPONDING-SOURCE.txt':
            # Reproduce Windows text-mode newline translation on any test host.
            return path.write_bytes(data.replace('\n', '\r\n').encode('utf-8'))
        return original(path, data, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', windows_text_newlines)
    prefix = tmp_path / 'own-venv';prefix.mkdir()
    notice = module._venv_notices(prefix, resource)
    raw = (notice / 'CORRESPONDING-SOURCE.txt').read_bytes()
    assert b'\r' not in raw
    assert raw.endswith(b'\n')
    assert digest(raw) == module.read_json(notice / 'notice-inventory.json')['CORRESPONDING-SOURCE.txt']
    assert module._venv_notices(prefix, resource) == notice


@pytest.mark.parametrize('field,value', [
    ('production_integration_allowed', True), ('external_ax_client_tested', True),
    ('final_application_acceptance_passed', True), ('source_resource', {}),
    ('minimum_macos_version', '14.0'), ('qt_version', '6.13.0'),
    ('architecture', 'x86_64'), ('scope', 'production approved'),
])
def test_receipt_cannot_promote_preparation_to_production(resource, purelib, field, value):
    path = purelib / module.RECEIPT
    receipt = apply_synthetic_overlay(purelib, path, resource, context='explicit-user-venv')
    receipt[field] = value
    write_json(path, receipt)
    with pytest.raises(RuntimeError, match='receipt'):
        module.verify_overlay(purelib, receipt, policy=resource.policy)
    with pytest.raises(RuntimeError, match='receipt'):
        apply_synthetic_overlay(purelib, path, resource, context='explicit-user-venv')


@pytest.mark.parametrize('step', ['partial-plugin-copy', 'partial-json-write', 'hardlink-backup'])
def test_partial_io_failure_leaves_no_temporary_or_modified_files(resource, purelib, monkeypatch, step):
    before = module.archives.inventory(purelib)
    def partial_copy(source, destination, *args):
        destination.write(b'partial plugin')
        raise OSError('disk full')
    def partial_json(value, destination, **kwargs):
        destination.write('{"partial":')
        raise OSError('disk full')
    def failed_link(*args):
        raise OSError('disk full')
    if step == 'partial-plugin-copy': monkeypatch.setattr(module.shutil, 'copyfileobj', partial_copy)
    if step == 'partial-json-write': monkeypatch.setattr(module.json, 'dump', partial_json)
    if step == 'hardlink-backup': monkeypatch.setattr(module.os, 'link', failed_link)
    with pytest.raises(OSError, match='disk full'):
        apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    assert module.archives.inventory(purelib) == before


def test_failed_rollback_preserves_original_recovery_bytes(resource, purelib, monkeypatch):
    replace = module.os.replace
    calls = []
    def fail_rollback(source, destination):
        calls.append((source, destination))
        if len(calls) == 2:
            raise OSError('simulated filesystem failure during rollback')
        return replace(source, destination)
    def failed_receipt(*args):
        raise OSError('simulated receipt disk full')
    monkeypatch.setattr(module.os, 'replace', fail_rollback)
    monkeypatch.setattr(module, '_atomic_json', failed_receipt)
    with pytest.raises(RuntimeError, match='Exact original plugin preserved'):
        apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    backups = list((purelib / module.PLUGIN).parent.glob('.vse-qt-original-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == b'original'
    assert not (purelib / module.RECEIPT).exists()


def native_receipt(receipt, policy):
    receipt = copy.deepcopy(receipt)
    root = receipt['purelib']
    receipt['native_load_probe_passed'] = True
    receipt['native_load_probe'] = {'qt': policy['qt_version'], 'backend': 'cocoa',
        'accessibility_active': True, 'external_ax_client_tested': False, 'scope': module.NATIVE_LOAD_SCOPE,
        'images': [root + '/' + module.PLUGIN.as_posix()] + [
            root + f'/PySide6/Qt/lib/{name}.framework/Versions/A/{name}' for name in ('QtCore', 'QtGui')]}
    return receipt


def test_aggregate_receipt_validation_needs_no_mac_files_or_execution(resource, purelib, monkeypatch):
    receipt = apply_synthetic_overlay(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    with pytest.raises(RuntimeError, match='native-load'):
        module.validate_receipt_identity(receipt, policy=resource.policy, require_native_load=True)
    receipt = native_receipt(receipt, resource.policy)
    shutil.rmtree(purelib)
    def forbidden(*args, **kwargs):
        pytest.fail('Portable receipt validation must not run or read a Mac runtime')
    monkeypatch.setattr(module, 'inspect_target', forbidden)
    monkeypatch.setattr(module.subprocess, 'check_output', forbidden)
    result = module.validate_receipt_identity(receipt, policy=resource.policy, require_native_load=True)
    assert result['receipt_identity_verified'] and result['native_load_claim_checked']
    assert result['external_ax_client_tested'] is False


@pytest.mark.parametrize('case', ['foreign-image', 'duplicate-image', 'false-external-claim', 'inactive',
                                  'no-records', 'bad-record-path', 'missing-proof', 'relative-purelib'])
def test_portable_aggregate_rejects_unbound_or_overstated_proof(resource, purelib, case):
    receipt = native_receipt(apply_synthetic_overlay(purelib, purelib / module.RECEIPT,
                                          resource, context='explicit-user-venv'), resource.policy)
    if case == 'foreign-image': receipt['native_load_probe']['images'][0] = '/foreign/libqcocoa.dylib'
    if case == 'duplicate-image': receipt['native_load_probe']['images'].append(receipt['native_load_probe']['images'][0])
    if case == 'false-external-claim': receipt['native_load_probe']['external_ax_client_tested'] = True
    if case == 'inactive': receipt['native_load_probe']['accessibility_active'] = False
    if case == 'no-records': receipt['original_RECORD'] = {}
    if case == 'bad-record-path': receipt['original_RECORD'] = {'../system/RECORD': 'a' * 64}
    if case == 'missing-proof': receipt.pop('native_load_probe')
    if case == 'relative-purelib': receipt['purelib'] = '../site-packages'
    with pytest.raises(RuntimeError):
        module.validate_receipt_identity(receipt, policy=resource.policy, require_native_load=True)


def test_private_apply_records_its_actual_host_path_without_conversion(resource, purelib):
    # Public entry points are macOS-only. On Windows this private synthetic
    # operation is not a valid Mac-origin receipt until the test maps it above.
    receipt = module._apply(purelib, purelib / module.RECEIPT, resource, context='explicit-user-venv')
    assert receipt['purelib'] == str(purelib)
    if os.name == 'nt':
        with pytest.raises(RuntimeError, match='canonical macOS'):
            module.validate_receipt_identity(receipt, policy=resource.policy)
    else:
        module.validate_receipt_identity(receipt, policy=resource.policy)


@pytest.mark.parametrize('path', [
    r'D:\runner\venv\Lib\site-packages', 'D:/runner/venv/Lib/site-packages',
    r'\\server\share\venv', '/Users/ci/../foreign', '/Users/ci//venv',
])
def test_windows_or_noncanonical_origin_paths_remain_rejected(resource, purelib, path):
    receipt = apply_synthetic_overlay(purelib, purelib / module.RECEIPT,
                                      resource, context='explicit-user-venv')
    receipt['purelib'] = path
    with pytest.raises(RuntimeError, match='canonical macOS'):
        module.validate_receipt_identity(receipt, policy=resource.policy)


def test_synthetic_receipt_has_mac_origin_but_checks_real_relocated_bytes(resource, purelib, tmp_path):
    receipt = apply_synthetic_overlay(purelib, purelib / module.RECEIPT,
                                      resource, context='explicit-user-venv')
    assert receipt['purelib'] == SYNTHETIC_MACOS_PURELIB
    relocated = tmp_path / 'relocated-runtime'
    shutil.move(purelib, relocated)
    module.verify_overlay(relocated, receipt, policy=resource.policy)
    (relocated / module.PLUGIN).write_bytes(b'changed after relocation')
    with pytest.raises(RuntimeError):
        module.verify_overlay(relocated, receipt, policy=resource.policy)
