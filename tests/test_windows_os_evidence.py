"""Read-only OS proof is separate from ordinary/static runtime allowlists."""
import copy
import json
import ntpath
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import windows_os_evidence as evidence


DEFENDER = r'C:\ProgramData\Microsoft\Windows Defender\Platform\4.18.25090.11-0\MpOav.dll'
SYSTEM_DLL = r'C:\Windows\System32\directxdatabasehelper.dll'


def proof(path=DEFENDER):
    return {
        'schema': 1, 'registry_modified': False, 'errors': [], 'amsi_errors': [],
        'windows_directory_api': r'C:\Windows',
        'roots': {'windows': r'C:\Windows', 'system32': r'C:\Windows\System32',
                  'program_data': r'C:\ProgramData', 'program_files': r'C:\Program Files'},
        'amsi_providers': [{'hive': 'HKLM', 'view': 64,
                            'clsid': '{2781761E-28E0-4109-99FE-B9D127C57AFE}',
                            'inproc_path': DEFENDER, 'threading_model': 'Both', 'value_kind': 'String', 'errors': []}],
        'files': [{'requested_path': path, 'canonical_path': path, 'errors': [], 'reparse_verified': True,
                   'sha256_before': 'a' * 64, 'sha256_after': 'a' * 64,
                   'version_info': {'company_name': 'Microsoft Corporation', 'original_filename': ntpath.basename(path)},
                   'signature': {'status': 'Valid', 'signature_type': 'Authenticode', 'is_os_binary': True,
                                 'signer': {'simple_name': 'Microsoft Windows',
                                            'subject': 'CN=Microsoft Windows, O=Microsoft Corporation, C=US',
                                            'issuer': 'CN=Microsoft Code Signing PCA 2011, O=Microsoft Corporation',
                                            'thumbprint': '123456'}}}],
    }


def test_registered_signed_defender_is_security_not_static_os_exemption():
    result = evidence.classify_loaded_windows_component(DEFENDER, proof())
    assert result['allowed']
    assert result['category'] == 'registered_microsoft_defender_amsi_provider'
    assert 'Loaded-process' in result['scope']
    from scripts.standalone_runtime_probe import library_violation
    assert library_violation(DEFENDER, r'C:\VSE', 'Windows')
    # Defender platform updates need not be marked as an OS release binary.
    data = proof()
    data['files'][0]['signature']['is_os_binary'] = False
    assert evidence.classify_loaded_windows_component(DEFENDER, data)['allowed']


@pytest.mark.parametrize('name', ['directxdatabasehelper.dll', 'coloradapterclient.dll'])
def test_exact_system32_component_requires_os_signature(name):
    path = 'C:\\Windows\\System32\\' + name
    data = proof(path)
    assert evidence.classify_loaded_windows_component(path, data)['category'] == 'signed_windows_os_component'
    data['files'][0]['signature']['is_os_binary'] = False
    assert not evidence.classify_loaded_windows_component(path, data)['allowed']


@pytest.mark.parametrize('change', [
    'no-signature', 'invalid-signature', 'no-signer', 'wrong-publisher', 'misleading-company',
    'missing-issuer', 'no-thumbprint', 'unsupported-signature-kind', 'unstable-hash', 'missing-hash',
    'path-mismatch', 'missing-reparse-check', 'renamed-signed-file', 'missing-original-name',
    'file-error', 'collector-error', 'registry-error', 'registry-modified',
    'missing-provider', 'wrong-registration', 'wrong-hive', 'wrong-view', 'wrong-threading',
    'invalid-clsid', 'different-valid-clsid', 'wrong-value-kind', 'provider-error', 'duplicate-provider', 'duplicate-file', 'wrong-os-root',
])
def test_defender_proof_fails_closed(change):
    data = proof()
    row = data['files'][0]
    signature = row['signature']
    provider = data['amsi_providers'][0]
    if change == 'no-signature': row.pop('signature')
    elif change == 'invalid-signature': signature['status'] = 'HashMismatch'
    elif change == 'no-signer': signature['signer'] = None
    elif change == 'wrong-publisher': signature['signer']['simple_name'] = 'Not Microsoft'
    elif change == 'misleading-company': signature['signer']['subject'] = 'CN=Microsoft Windows, O=Evil Publisher'
    elif change == 'missing-issuer': signature['signer'].pop('issuer')
    elif change == 'no-thumbprint': signature['signer'].pop('thumbprint')
    elif change == 'unsupported-signature-kind': signature['signature_type'] = 'None'
    elif change == 'unstable-hash': row['sha256_after'] = 'b' * 64
    elif change == 'missing-hash': row.pop('sha256_before')
    elif change == 'path-mismatch': row['canonical_path'] = r'C:\Temp\MpOav.dll'
    elif change == 'missing-reparse-check': row.pop('reparse_verified')
    elif change == 'renamed-signed-file': row['version_info']['original_filename'] = 'unrelated-microsoft.dll'
    elif change == 'missing-original-name': row['version_info'].pop('original_filename')
    elif change == 'file-error': row['errors'] = ['access denied']
    elif change == 'collector-error': data['errors'] = ['failed']
    elif change == 'registry-error': data['amsi_errors'] = ['access denied']
    elif change == 'registry-modified': data['registry_modified'] = True
    elif change == 'missing-provider': data['amsi_providers'] = []
    elif change == 'wrong-registration': provider['inproc_path'] = r'C:\Temp\MpOav.dll'
    elif change == 'wrong-hive': provider['hive'] = 'HKCU'
    elif change == 'wrong-view': provider['view'] = 32
    elif change == 'wrong-threading': provider['threading_model'] = 'Apartment'
    elif change == 'invalid-clsid': provider['clsid'] = 'not-a-clsid'
    elif change == 'different-valid-clsid': provider['clsid'] = '{aaaaaaaa-0000-1111-2222-bbbbbbbbbbbb}'
    elif change == 'wrong-value-kind': provider['value_kind'] = 'MultiString'
    elif change == 'provider-error': provider['errors'] = ['missing value']
    elif change == 'duplicate-provider': data['amsi_providers'].append(copy.deepcopy(provider))
    elif change == 'duplicate-file': data['files'].append(copy.deepcopy(row))
    elif change == 'wrong-os-root': data['windows_directory_api'] = r'D:\Windows'
    result = evidence.classify_loaded_windows_component(DEFENDER, data)
    assert not result['allowed'], change
    assert result['reason']


@pytest.mark.parametrize('path', [
    r'C:\ProgramData\Other\MpOav.dll',
    r'C:\ProgramData\Microsoft\Windows Defender\Platform-evil\4.18.25090.11-0\MpOav.dll',
    r'C:\ProgramData\Microsoft\Windows Defender\Platform\4.18.25090.11-0\nested\MpOav.dll',
    r'C:\ProgramData\Microsoft\Windows Defender\Platform\not-a-version\MpOav.dll',
    r'C:\Program Files\Windows Defender-other\MpOav.dll',
    r'C:\Windows\System32-evil\directxdatabasehelper.dll',
    r'C:\Windows\System32\nested\coloradapterclient.dll',
    r'C:\Windows\System32\coloradapterclient_other.dll',
    r'C:\Windows\System32\vcomp140.dll',
    r'\\server\share\MpOav.dll',
])
def test_wrong_paths_names_and_vendor_dlls_are_never_exempt(path):
    data = proof(path)
    data['amsi_providers'][0]['inproc_path'] = path
    assert not evidence.classify_loaded_windows_component(path, data)['allowed']


def test_documented_program_files_fallback_requires_exact_registration():
    path = r'C:\Program Files\Windows Defender\MpOav.dll'
    data = proof(path)
    assert not evidence.classify_loaded_windows_component(path, data)['allowed']
    data['amsi_providers'][0]['inproc_path'] = path
    assert evidence.classify_loaded_windows_component(path, data)['allowed']


def test_collection_is_cached_per_distinct_case_normalized_path(monkeypatch):
    calls = []
    def collect(paths, timeout):
        calls.append((paths, timeout))
        result = proof(paths[0])
        return result
    monkeypatch.setattr(evidence, '_collect', collect)
    collector = evidence.WindowsOSEvidenceCollector(timeout=12)
    first = collector.collect([DEFENDER, DEFENDER.lower()])
    second = collector.collect([DEFENDER.lower()])
    assert len(calls) == 1 and len(first['files']) == len(second['files']) == 1
    assert calls[0][1] == 12
    assert evidence.classify_loaded_windows_component(DEFENDER, second)['allowed']


@pytest.mark.parametrize('error', [OSError('read denied'), subprocess.TimeoutExpired('powershell', 3), ValueError('bad JSON')])
def test_collector_failures_are_retained_and_not_retried_silently(monkeypatch, error):
    calls = []
    def fail(paths, timeout):
        calls.append(paths)
        raise error
    monkeypatch.setattr(evidence, '_collect', fail)
    collector = evidence.WindowsOSEvidenceCollector(timeout=3)
    result = collector.collect([DEFENDER])
    collector.collect([DEFENDER])
    assert len(calls) == 1
    assert result['files'][0]['errors']
    assert not evidence.classify_loaded_windows_component(DEFENDER, result)['allowed']


def test_collector_uses_absolute_os_powershell_stdin_json_and_no_shell(monkeypatch):
    calls = []
    monkeypatch.setenv('PATH', '')
    monkeypatch.setenv('SystemRoot', r'C:\Untrusted')
    monkeypatch.setenv('ProgramData', r'C:\Untrusted')
    monkeypatch.setattr(evidence, '_windows_directory', lambda: r'C:\Windows')
    monkeypatch.setattr(evidence, '_canonical_path', lambda path: path)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(proof()), stderr='')
    monkeypatch.setattr(evidence.subprocess, 'run', run)
    result = evidence.collect_windows_os_evidence([DEFENDER], timeout=5)
    command, kwargs = calls[0]
    assert command[0] == r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    assert command[1:4] == ['-NoLogo', '-NoProfile', '-NonInteractive']
    assert command[-1] == evidence.POWERSHELL_COLLECTOR
    assert DEFENDER not in command[-1]
    assert json.loads(kwargs['input']) == {'paths': [DEFENDER]}
    assert kwargs['timeout'] == 5 and not kwargs.get('shell', False)
    assert evidence.classify_loaded_windows_component(DEFENDER, result)['allowed']


def test_script_collects_only_requested_files_and_read_only_native_registry():
    script = evidence.POWERSHELL_COLLECTOR
    assert 'Registry64' in script and "OpenSubKey('SOFTWARE\\Microsoft\\AMSI\\Providers', $false)" in script
    assert 'DoNotExpandEnvironmentNames' in script
    assert '[Environment+SpecialFolder]::CommonApplicationData' in script
    assert '-LiteralPath $path' in script and 'sha256_before' in script and 'sha256_after' in script
    assert 'No-Reparse-Ancestors $path' in script
    for forbidden in ('Set-ItemProperty', 'New-Item', 'SetValue(', 'CreateSubKey', 'Set-MpPreference',
                      'Invoke-WebRequest', 'DownloadFile', 'Get-ChildItem', '-ExecutionPolicy'):
        assert forbidden not in script


@pytest.mark.parametrize('path', [r'\\server\share\MpOav.dll', r'C:\Windows\System32\vcomp140.dll', r'C:\file.dll:stream'])
def test_unsupported_collection_targets_do_not_invoke_powershell(monkeypatch, path):
    monkeypatch.setattr(evidence, '_collect', lambda *args: pytest.fail('must not collect'))
    with pytest.raises(ValueError):
        evidence.collect_windows_os_evidence([path])


def test_malformed_collector_output_fails_closed(monkeypatch):
    monkeypatch.setattr(evidence, '_windows_directory', lambda: r'C:\Windows')
    monkeypatch.setattr(evidence.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    result = evidence.collect_windows_os_evidence([SYSTEM_DLL])
    assert result['files'][0]['errors']
    assert not evidence.classify_loaded_windows_component(SYSTEM_DLL, result)['allowed']


NEW_DEFENDER = DEFENDER.replace('4.18.25090.11-0', '4.18.25100.4-0')


def transition_pair():
    before = proof()
    before['files'][0]['version_info']['product_version'] = '4.18.25090.11'
    after = proof(NEW_DEFENDER)
    after['amsi_providers'][0]['inproc_path'] = NEW_DEFENDER
    after['files'][0]['version_info']['product_version'] = '4.18.25100.4'
    after['files'][0]['sha256_before'] = after['files'][0]['sha256_after'] = 'b' * 64
    after['files'].append(copy.deepcopy(before['files'][0]))
    return before, after


def test_proven_registration_transition_does_not_rewrite_either_original_snapshot():
    before, after = transition_pair()
    originals = copy.deepcopy((before, after))
    # Reproduce the original post-workload-only failure first.
    assert not evidence.classify_loaded_windows_component(DEFENDER, after)['allowed']
    result = evidence.classify_defender_registration_transition(DEFENDER, before, after)
    assert result['allowed'] and result['category'] == 'observed_defender_amsi_registration_transition'
    assert result['sha256'] == 'a' * 64 and result['registered_after_sha256'] == 'b' * 64
    assert result['registered_before'] == DEFENDER and result['registered_after'] == NEW_DEFENDER
    assert (before, after) == originals


@pytest.mark.parametrize('change', [
    'before-already-new', 'no-before-provider', 'invalid-before-signature', 'before-registry-error',
    'old-file-changed', 'old-file-unstable', 'old-file-unsigned', 'old-file-reparse',
    'no-current-file', 'current-unsigned', 'current-hash-unstable', 'current-provider-error',
    'current-registry-error', 'duplicate-current-provider', 'duplicate-before-provider',
    'duplicate-old-file', 'wrong-roots', 'missing-before-version', 'wrong-old-version',
    'wrong-new-version', 'backward-version', 'unchanged-registration', 'wrong-name',
])
def test_transition_requires_exact_before_registration_and_both_authenticated_stable_files(change):
    before, after = transition_pair()
    old, new = after['files'][1], after['files'][0]
    path = DEFENDER
    if change == 'before-already-new': before = copy.deepcopy(after)
    elif change == 'no-before-provider': before['amsi_providers'] = []
    elif change == 'invalid-before-signature': before['files'][0]['signature']['status'] = 'UnknownError'
    elif change == 'before-registry-error': before['amsi_errors'] = ['unreadable']
    elif change == 'old-file-changed': old['sha256_before'] = old['sha256_after'] = 'c' * 64
    elif change == 'old-file-unstable': old['sha256_after'] = 'c' * 64
    elif change == 'old-file-unsigned': old['signature']['status'] = 'NotSigned'
    elif change == 'old-file-reparse': old['reparse_verified'] = False
    elif change == 'no-current-file': after['files'].remove(new)
    elif change == 'current-unsigned': new['signature']['status'] = 'NotSigned'
    elif change == 'current-hash-unstable': new['sha256_after'] = 'c' * 64
    elif change == 'current-provider-error': after['amsi_providers'][0]['errors'] = ['unreadable']
    elif change == 'current-registry-error': after['amsi_errors'] = ['unreadable']
    elif change == 'duplicate-current-provider': after['amsi_providers'].append(copy.deepcopy(after['amsi_providers'][0]))
    elif change == 'duplicate-before-provider': before['amsi_providers'].append(copy.deepcopy(before['amsi_providers'][0]))
    elif change == 'duplicate-old-file': after['files'].append(copy.deepcopy(old))
    elif change == 'wrong-roots': before['roots']['program_files'] = r'D:\Program Files'
    elif change == 'missing-before-version': before['files'][0]['version_info'].pop('product_version')
    elif change == 'wrong-old-version': old['version_info']['product_version'] = '4.18.25100.4'
    elif change == 'wrong-new-version': new['version_info']['product_version'] = '4.18.25090.11'
    elif change == 'backward-version':
        before, after = transition_pair()
        path = NEW_DEFENDER
        before = copy.deepcopy(after)
        before['amsi_providers'][0]['inproc_path'] = NEW_DEFENDER
        after['amsi_providers'][0]['inproc_path'] = DEFENDER
    elif change == 'unchanged-registration': after['amsi_providers'][0]['inproc_path'] = DEFENDER
    elif change == 'wrong-name': path = SYSTEM_DLL
    assert not evidence.classify_defender_registration_transition(path, before, after)['allowed'], change


def test_registered_snapshot_is_bounded_and_uses_the_same_native_read_only_collector(monkeypatch):
    calls = []
    monkeypatch.setattr(evidence, '_windows_directory', lambda: r'C:\Windows')
    monkeypatch.setattr(evidence, '_canonical_path', lambda path: path)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(proof()), stderr='')
    monkeypatch.setattr(evidence.subprocess, 'run', run)
    result = evidence.collect_windows_security_snapshot(timeout=7)
    assert evidence.classify_loaded_windows_component(DEFENDER, result)['allowed']
    command, kwargs = calls[0]
    assert command[0] == r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    assert json.loads(kwargs['input']) == {'paths': [], 'include_registered_defender': True}
    assert kwargs['timeout'] == 7 and not kwargs.get('shell', False)
    assert 'Only the exact native Defender CLSID' in command[-1]


@pytest.mark.parametrize('change', ['extra-row', 'duplicate-row', 'timeout', 'unsupported-name', 'too-many'])
def test_registered_snapshot_rejects_unexpected_rows_and_unbounded_inputs(monkeypatch, change):
    monkeypatch.setattr(evidence, '_windows_directory', lambda: r'C:\Windows')
    monkeypatch.setattr(evidence, '_canonical_path', lambda path: path)
    data = proof()
    if change == 'extra-row': data['files'].append(proof(SYSTEM_DLL)['files'][0])
    if change == 'duplicate-row': data['files'].append(copy.deepcopy(data['files'][0]))
    monkeypatch.setattr(evidence.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr=''))
    if change in ('extra-row', 'duplicate-row'):
        assert evidence.collect_windows_security_snapshot()['errors']
    else:
        with pytest.raises(ValueError):
            if change == 'timeout': evidence.collect_windows_security_snapshot(timeout=121)
            elif change == 'unsupported-name': evidence.collect_windows_security_snapshot([r'C:\Windows\System32\vcruntime140.dll'])
            else: evidence.collect_windows_security_snapshot([fr'C:\folder{i}\MpOav.dll' for i in range(33)])


def audit_fixture(tmp_path):
    from scripts import standalone_runtime_probe as probe
    bundle, directory = tmp_path / 'bundle', tmp_path / 'reports'
    (bundle / 'scripts').mkdir(parents=True)
    (bundle / 'scripts/standalone_runtime_probe.py').write_bytes(Path(probe.__file__).read_bytes())
    return probe, bundle, directory


def write_audit_process(bundle, directory):
    record = {'pid': 1, 'system': 'Windows', 'executable': str(bundle / 'runtime/python.exe'),
              'prefix': str(bundle / 'runtime'), 'images': [DEFENDER], 'snapshots': 1,
              'probe_errors': [], 'windows_root': r'C:\Windows', 'modules': {}, 'argv': ['gui.py']}
    (directory / 'process-1.json').write_text(json.dumps(record))


def test_verifier_binds_before_snapshot_prior_to_workloads_and_preserves_both_raw_snapshots(tmp_path, monkeypatch):
    probe, bundle, directory = audit_fixture(tmp_path)
    before, after = transition_pair()
    calls = []
    def snapshot(paths=()):
        calls.append(list(paths))
        assert (directory / 'process-1.json').exists() == bool(paths)
        return copy.deepcopy(after if paths else before)
    monkeypatch.setattr(probe.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(evidence, 'collect_windows_security_snapshot', snapshot)
    probe.audit_environment(bundle, directory, capture_windows_security=True)
    baseline_raw = (directory / 'windows-defender-before.json').read_bytes()
    write_audit_process(bundle, directory)
    result = probe.summarize_reports(bundle, directory)
    assert result['passed']
    assert calls == [[], [DEFENDER]]
    assert result['observed_os_security_components'][0]['category'] == 'observed_defender_amsi_registration_transition'
    assert (directory / 'windows-defender-before.json').read_bytes() == baseline_raw
    assert json.loads((directory / 'windows-os-security-evidence.json').read_text()) == after


@pytest.mark.parametrize('change', ['unbound-file', 'tampered-file', 'missing-file', 'already-new-before', 'before-failed'])
def test_verifier_cannot_accept_a_fabricated_stale_or_insufficient_baseline(tmp_path, monkeypatch, change):
    probe, bundle, directory = audit_fixture(tmp_path)
    before, after = transition_pair()
    if change == 'already-new-before': before = copy.deepcopy(after)
    if change == 'before-failed': before = {'schema': 1, 'errors': ['collector timeout'], 'files': []}
    monkeypatch.setattr(probe.platform, 'system', lambda: 'Windows')
    monkeypatch.setattr(evidence, 'collect_windows_security_snapshot', lambda paths=(): copy.deepcopy(after if paths else before))
    monkeypatch.setattr(evidence, 'collect_windows_os_evidence', lambda paths: copy.deepcopy(after))
    probe.audit_environment(bundle, directory, capture_windows_security=change != 'unbound-file')
    if change == 'unbound-file':
        (directory / 'windows-defender-before.json').write_text(json.dumps({'snapshot': before}))
    elif change == 'tampered-file':
        with (directory / 'windows-defender-before.json').open('a') as stream: stream.write(' ')
    elif change == 'missing-file':
        (directory / 'windows-defender-before.json').unlink()
    write_audit_process(bundle, directory)
    with pytest.raises(RuntimeError, match='Standalone runtime audit failed'):
        probe.summarize_reports(bundle, directory)
    result = json.loads((directory / 'summary.json').read_text())
    assert not result['passed'] and not result['observed_os_security_components']


@pytest.mark.parametrize('system,enabled', [('Darwin', True), ('Linux', True), ('Windows', False)])
def test_baseline_collection_only_runs_for_explicit_windows_archive_verification(tmp_path, monkeypatch, system, enabled):
    probe, bundle, directory = audit_fixture(tmp_path)
    monkeypatch.setattr(probe.platform, 'system', lambda: system)
    monkeypatch.setattr(evidence, 'collect_windows_security_snapshot', lambda *a: pytest.fail('must not run native collector'))
    probe.audit_environment(bundle, directory, capture_windows_security=enabled)
    assert not (directory / 'windows-defender-before.json').exists()


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Server PowerShell parser; never run on another OS')
def test_native_server_powershell_parser_accepts_the_complete_read_only_collector():
    executable = ntpath.join(evidence._windows_directory(), 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    parser = r'''$ErrorActionPreference='Stop'; $tokens=$null; $parseErrors=$null
[void][System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { $parseErrors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }; exit 1 }
'''
    result = subprocess.run([executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', parser],
                            input=evidence.POWERSHELL_COLLECTOR, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
