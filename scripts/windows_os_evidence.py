"""Read-only, per-run evidence for narrowly selected Windows loaded images.

This is NOT a PE import allowlist. Only the loaded-process audit may use the
pure classifier, with freshly collected evidence from this trusted helper.
No protection settings, registry keys, certificates, or OS files are changed.
No downloads or explicit network/chain-update requests are issued. Windows'
Authenticode implementation owns its certificate trust/cache policy.

Primary sources:
https://learn.microsoft.com/en-us/exchange/antispam-and-antimalware/amsi-integration-with-exchange
https://learn.microsoft.com/en-us/windows/win32/amsi/dev-audience
https://learn.microsoft.com/en-us/defender-endpoint/collect-diagnostic-data
https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/get-authenticodesignature
https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.signature
"""
from __future__ import annotations

import ctypes
import json
import ntpath
from pathlib import Path
import re
import subprocess
import sys


CANDIDATE_NAMES = frozenset({'mpoav.dll', 'directxdatabasehelper.dll', 'coloradapterclient.dll'})
SIGNED_OS_NAMES = CANDIDATE_NAMES - {'mpoav.dll'}
MICROSOFT_SIGNERS = frozenset({'Microsoft Windows', 'Microsoft Corporation', 'Microsoft Windows Publisher'})
# Defender provider shown by Microsoft's Exchange team, not an arbitrary CLSID:
# https://techcommunity.microsoft.com/blog/exchange/more-about-amsi-integration-with-exchange-server/2572371
DEFENDER_PROVIDER_CLSID = '{2781761E-28E0-4109-99FE-B9D127C57AFE}'
_SHA256 = re.compile(r'[a-fA-F0-9]{64}\Z')
_VERSION_DIRECTORY = re.compile(r'\d+\.\d+\.\d+\.\d+-\d+\Z')


# Inputs travel over stdin JSON, never interpolation into PowerShell source.
# Native HKLM/Registry64 is deliberately used instead of the HKCR/HKCU overlay.
POWERSHELL_COLLECTOR = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PSModulePath = [IO.Path]::Combine($PSHOME, 'Modules')
Import-Module ([IO.Path]::Combine($PSHOME, 'Modules', 'Microsoft.PowerShell.Security', 'Microsoft.PowerShell.Security.psd1')) -ErrorAction Stop
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$result = [ordered]@{schema=1; files=@(); roots=@{}; amsi_providers=@(); errors=@(); amsi_errors=@(); registry_modified=$false}
function Hash-File([string] $path) {
    $stream = [IO.File]::OpenRead($path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Certificate-Record($certificate) {
    if ($null -eq $certificate) { return $null }
    return [ordered]@{
        subject=$certificate.Subject; issuer=$certificate.Issuer; thumbprint=$certificate.Thumbprint
        simple_name=$certificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
        not_before=$certificate.NotBefore.ToUniversalTime().ToString('o')
        not_after=$certificate.NotAfter.ToUniversalTime().ToString('o')
    }
}
function Allowed-Layout([string] $path) {
    $parent = [IO.Path]::GetDirectoryName($path)
    $name = [IO.Path]::GetFileName($path)
    if ($name -in @('directxdatabasehelper.dll', 'coloradapterclient.dll')) {
        return [string]::Equals($parent, $result.roots.system32, [StringComparison]::OrdinalIgnoreCase)
    }
    if ($name -ine 'MpOav.dll') { return $false }
    $legacy = [IO.Path]::Combine($result.roots.program_files, 'Windows Defender')
    if ([string]::Equals($parent, $legacy, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    $platform = [IO.Path]::Combine($result.roots.program_data, 'Microsoft', 'Windows Defender', 'Platform')
    return ([string]::Equals([IO.Path]::GetDirectoryName($parent), $platform, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($parent) -match '^\d+\.\d+\.\d+\.\d+-\d+$')
}
function No-Reparse-Ancestors([string] $path) {
    $current = $path
    while ($current) {
        if (([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw ('Reparse point is not accepted for OS evidence: ' + $current)
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}
try {
    if (-not [Environment]::Is64BitProcess) { throw 'Native 64-bit PowerShell is required' }
    $result.roots = [ordered]@{
        windows=[Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
        system32=[Environment]::SystemDirectory
        program_data=[Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
        program_files=[Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFiles)
    }
    foreach ($value in $result.roots.Values) {
        if (-not $value -or $value -notmatch '^[A-Za-z]:\\') { throw 'Missing/nonlocal OS KnownFolder root' }
    }
} catch { $result.errors += $_.Exception.Message }
if (@($request.paths | Where-Object { [IO.Path]::GetFileName($_) -ieq 'MpOav.dll' }).Count) {
    $hive = $null; $providers = $null
    try {
        $hive = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry64)
        $providers = $hive.OpenSubKey('SOFTWARE\Microsoft\AMSI\Providers', $false)
        if ($null -eq $providers) { throw 'Native HKLM AMSI provider registry is missing' }
        foreach ($clsid in $providers.GetSubKeyNames()) {
            $record = [ordered]@{hive='HKLM'; view=64; clsid=$clsid; inproc_path=$null; threading_model=$null; errors=@()}
            $key = $null
            try {
                if ($clsid -notmatch '^\{[0-9a-fA-F-]{36}\}$') { throw 'Invalid registered provider CLSID' }
                $key = $hive.OpenSubKey(('SOFTWARE\Classes\CLSID\' + $clsid + '\InprocServer32'), $false)
                if ($null -eq $key) { throw 'Provider InprocServer32 registry is missing' }
                $record.raw_value = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $record.value_kind = [string]$key.GetValueKind('')
                if ($record.value_kind -notin @('String', 'ExpandString')) { throw 'Provider InprocServer32 has a non-string registry type' }
                $value = [string]$record.raw_value
                # Expand only documented OS folder tokens, from KnownFolder APIs.
                $value = [regex]::Replace($value, '(?i)%ProgramData%', { param($m) $result.roots.program_data })
                $value = [regex]::Replace($value, '(?i)%ProgramFiles%', { param($m) $result.roots.program_files })
                if ($value.StartsWith('"') -and $value.EndsWith('"')) { $value = $value.Substring(1, $value.Length - 2) }
                if ($value -notmatch '^[A-Za-z]:\\' -or $value.Contains('%') -or $value.Contains('"')) { throw 'Provider path is not a single local absolute filename' }
                $record.inproc_path = [IO.Path]::GetFullPath($value)
                $record.threading_model = $key.GetValue('ThreadingModel')
            } catch { $record.errors += $_.Exception.Message }
            finally { if ($null -ne $key) { $key.Dispose() } }
            $result.amsi_providers += $record
        }
    } catch { $result.amsi_errors += $_.Exception.Message }
    finally {
        if ($null -ne $providers) { $providers.Dispose() }
        if ($null -ne $hive) { $hive.Dispose() }
    }
}
foreach ($requested in $request.paths) {
    $record = [ordered]@{requested_path=$requested; canonical_path=$null; errors=@()}
    try {
        if ($result.errors.Count) { throw 'OS root evidence is unavailable' }
        if ($requested -notmatch '^[A-Za-z]:\\' -or $requested.Substring(2).Contains(':')) { throw 'Only local absolute file paths are accepted' }
        $path = [IO.Path]::GetFullPath($requested)
        if (-not (Allowed-Layout $path)) { throw 'Requested file is outside exact supported OS component layouts' }
        No-Reparse-Ancestors $path
        $record.reparse_verified = $true
        $record.canonical_path = $path
        $record.sha256_before = Hash-File $path
        $info = [Diagnostics.FileVersionInfo]::GetVersionInfo($path)
        $record.version_info = [ordered]@{file_version=$info.FileVersion; product_version=$info.ProductVersion; company_name=$info.CompanyName; product_name=$info.ProductName; original_filename=$info.OriginalFilename}
        $sig = Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop
        $record.signature = [ordered]@{status=[string]$sig.Status; status_message=$sig.StatusMessage; signature_type=[string]$sig.SignatureType; is_os_binary=[bool]$sig.IsOSBinary; signer=(Certificate-Record $sig.SignerCertificate); timestamp_signer=(Certificate-Record $sig.TimeStamperCertificate)}
        $record.sha256_after = Hash-File $path
    } catch { $record.errors += $_.Exception.Message }
    $result.files += $record
}
[Console]::Out.Write(($result | ConvertTo-Json -Depth 10 -Compress))
'''


def _identity(path: str) -> str:
    if (not isinstance(path, str) or not re.match(r'^[A-Za-z]:[\\/]', path)
            or ':' in path[2:] or any(ord(char) < 32 for char in path)):
        raise ValueError('Expected local absolute Windows path')
    return ntpath.normcase(ntpath.normpath(path))


def _windows_directory() -> str:
    """Use the OS API, not inherited SystemRoot/PATH/ProgramData variables."""
    if sys.platform != 'win32':
        raise RuntimeError('Windows OS evidence requires a native Windows host')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = kernel.GetWindowsDirectoryW
    function.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    function.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = function(buffer, len(buffer))
    if not size or size >= len(buffer):
        raise RuntimeError('Cannot establish the OS Windows directory')
    return str(Path(buffer.value).resolve(strict=True))


def _canonical_path(path: str) -> str:
    _identity(path)  # Reject UNC/network targets before any filesystem access.
    resolved = str(Path(path).resolve(strict=True))
    _identity(resolved)
    return resolved


def _collect(paths: list[str], timeout: float) -> dict:
    windows = _windows_directory()
    executable = ntpath.join(windows, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    command = [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', POWERSHELL_COLLECTOR]
    # No shell, credential access, execution-policy override or path lookup.
    result = subprocess.run(command, input=json.dumps({'paths': paths}), text=True,
                            encoding='utf-8', errors='strict', capture_output=True,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f'OS evidence collector exited {result.returncode}: {result.stderr[:2000]}')
    if len(result.stdout) > 1024 * 1024:
        raise RuntimeError('OS evidence collector exceeded its bounded JSON output')
    evidence = json.loads(result.stdout.lstrip('\ufeff'))
    if evidence.get('schema') != 1 or not isinstance(evidence.get('files'), list):
        raise RuntimeError('Malformed OS evidence collector output')
    evidence['windows_directory_api'] = windows
    try:
        evidence['roots'] = {key: _canonical_path(value) for key, value in evidence.get('roots', {}).items()}
        if _identity(evidence.get('roots', {}).get('windows', '')) != _identity(windows):
            raise ValueError('KnownFolder Windows root differs from OS API root')
    except (OSError, ValueError) as error:
        evidence.setdefault('errors', []).append('OS root canonicalization failed: ' + str(error))
    requested = {_identity(path) for path in paths}
    for row in evidence['files']:
        if row.get('canonical_path') and not row.get('errors'):
            try:
                row['canonical_path'] = _canonical_path(row['canonical_path'])
            except (OSError, ValueError) as error:
                row.setdefault('errors', []).append('File canonicalization failed: ' + str(error))
    # Never inspect arbitrary third-party provider files. Only canonicalize the
    # registration already naming an explicitly requested loaded image.
    for provider in evidence.get('amsi_providers', []):
        if not provider.get('errors'):
            try:
                if _identity(provider.get('inproc_path', '')) in requested:
                    provider['inproc_path'] = _canonical_path(provider['inproc_path'])
            except (OSError, ValueError) as error:
                provider.setdefault('errors', []).append('Provider canonicalization failed: ' + str(error))
    return evidence


class WindowsOSEvidenceCollector:
    """Instantiate once per summary run; a requested path is inspected once."""
    def __init__(self, *, timeout: float = 60):
        if not 0 < timeout <= 120:
            raise ValueError('OS evidence timeout must be in (0, 120] seconds')
        self.timeout = timeout
        self._files: dict[str, dict] = {}
        self._metadata: dict = {'schema': 1, 'roots': {}, 'amsi_providers': [], 'errors': [], 'amsi_errors': [], 'registry_modified': False}

    def collect(self, paths) -> dict:
        requested = {}
        for path in paths:
            key = _identity(str(path))
            if ntpath.basename(key) not in CANDIDATE_NAMES:
                raise ValueError('Not an explicitly supported Windows evidence candidate: ' + str(path))
            requested.setdefault(key, str(path))
        if len(set(self._files) | set(requested)) > 32:
            raise ValueError('Too many distinct Windows OS evidence paths')
        unseen = {key: path for key, path in requested.items() if key not in self._files}
        if unseen:
            try:
                batch = _collect(list(unseen.values()), self.timeout)
                if self._metadata['roots'] and batch.get('roots') != self._metadata['roots']:
                    raise RuntimeError('OS KnownFolder roots changed during this summary')
                self._metadata = {key: value for key, value in batch.items() if key != 'files'}
                # Preserve earlier provider evidence if the new batch did not need AMSI.
                if not any(ntpath.basename(key) == 'mpoav.dll' for key in unseen):
                    self._metadata['amsi_providers'] = getattr(self, '_providers', [])
                    self._metadata['amsi_errors'] = getattr(self, '_provider_errors', [])
                else:
                    self._providers = batch.get('amsi_providers', [])
                    self._provider_errors = batch.get('amsi_errors', [])
                rows = {}
                for row in batch['files']:
                    key = _identity(row.get('requested_path', ''))
                    if key in rows or key not in unseen:
                        raise RuntimeError('Unexpected or duplicate OS evidence file row')
                    rows[key] = row
                for key, path in unseen.items():
                    self._files[key] = rows.get(key, {'requested_path': path, 'errors': ['Missing collector file row']})
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                for key, path in unseen.items():
                    self._files[key] = {'requested_path': path, 'errors': [f'{type(error).__name__}: {error}']}
        return dict(self._metadata, files=[self._files[key] for key in requested])


def collect_windows_os_evidence(paths, *, timeout: float = 60) -> dict:
    """One-shot convenience API; caller may retain the class for repeated queries."""
    return WindowsOSEvidenceCollector(timeout=timeout).collect(paths)


def classify_loaded_windows_component(path: str, evidence: dict) -> dict:
    """Pure fail-closed classification, NEVER permission for a static PE import."""
    denied = {'allowed': False, 'category': None, 'path': path}
    try:
        target = _identity(path)
        name = ntpath.basename(target)
        if name not in CANDIDATE_NAMES:
            raise ValueError('Not an exact supported OS evidence name')
        if evidence.get('schema') != 1 or evidence.get('errors') or evidence.get('registry_modified') is not False:
            raise ValueError('Missing/failed read-only collector evidence')
        roots = evidence.get('roots', {})
        windows = _identity(roots.get('windows', ''))
        if windows != _identity(evidence.get('windows_directory_api', '')):
            raise ValueError('OS root evidence mismatch')
        if _identity(roots.get('system32', '')) != ntpath.join(windows, 'system32'):
            raise ValueError('Invalid native System32 evidence')
        rows = [row for row in evidence.get('files', []) if _identity(row.get('requested_path', '')) == target]
        if len(rows) != 1 or rows[0].get('errors'):
            raise ValueError('Missing/ambiguous/failed file evidence')
        row = rows[0]
        if row.get('reparse_verified') is not True:
            raise ValueError('Missing file/ancestor reparse-point verification')
        if _identity(row.get('canonical_path', '')) != target:
            raise ValueError('Loaded file and canonical evidence path differ')
        digest = row.get('sha256_before', '')
        if not _SHA256.fullmatch(digest) or digest.lower() != str(row.get('sha256_after', '')).lower():
            raise ValueError('Missing or unstable file hash')
        signature = row.get('signature', {})
        signer = signature.get('signer') or {}
        if (signature.get('status') != 'Valid' or signature.get('signature_type') not in ('Authenticode', 'Catalog')
                or signer.get('simple_name') not in MICROSOFT_SIGNERS
                or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', signer.get('subject', ''))
                or not signer.get('issuer') or not signer.get('thumbprint')):
            raise ValueError('No valid authenticated Microsoft publisher evidence')
        original = row.get('version_info', {}).get('original_filename', '')
        if not isinstance(original, str) or ntpath.basename(original).lower() != name:
            raise ValueError('Signed original filename does not match the exact observed component')
        if name in SIGNED_OS_NAMES:
            if ntpath.dirname(target) != _identity(roots['system32']) or signature.get('is_os_binary') is not True:
                raise ValueError('Not an OS-signed exact System32 component')
            category = 'signed_windows_os_component'
        else:
            if evidence.get('amsi_errors'):
                raise ValueError('AMSI registry evidence failed')
            parent = ntpath.dirname(target)
            legacy = ntpath.join(_identity(roots.get('program_files', '')), 'windows defender')
            platform = ntpath.join(_identity(roots.get('program_data', '')), 'microsoft', 'windows defender', 'platform')
            if parent != legacy and not (ntpath.dirname(parent) == platform and _VERSION_DIRECTORY.fullmatch(ntpath.basename(parent))):
                raise ValueError('Not an exact Defender-managed platform layout')
            matches = [provider for provider in evidence.get('amsi_providers', [])
                       if not provider.get('errors') and provider.get('hive') == 'HKLM' and provider.get('view') == 64
                       and provider.get('threading_model') == 'Both'
                       and provider.get('value_kind') in ('String', 'ExpandString')
                       and provider.get('clsid', '').upper() == DEFENDER_PROVIDER_CLSID
                       and _identity(provider.get('inproc_path', '')) == target]
            if len(matches) != 1:
                raise ValueError('No unique native HKLM AMSI provider registration for this exact loaded file')
            category = 'registered_microsoft_defender_amsi_provider'
        return {'allowed': True, 'category': category, 'path': path, 'sha256': digest.lower(),
                'scope': 'Loaded-process OS/security component only; not a bundled application dependency'}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return dict(denied, reason=str(error))
