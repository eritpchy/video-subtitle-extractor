"""Explicit, hash-bound Qt Cocoa overlay for a venv or a CI package stage.

No installer, compiler, system-site mutation, accessibility disabling or release
approval lives here. Historical candidate evidence remains unchanged. A new
application/package still needs its own external-AX and release acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from scripts import macos_ax_archive as archives
from scripts.build_macos_qt_accessibility import macho_arm64, validate_link_contract

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / 'native/qt/macos-qt-overlay.json'
PLUGIN = Path('PySide6/Qt/plugins/platforms/libqcocoa.dylib')
RECEIPT = 'vse-qt-ax-overlay.json'
RECEIPT_SCOPE = 'Library preparation only; not application, external AX or release acceptance'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError(f'Missing or unsafe JSON: {path}')
    result = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return result


def load_policy() -> dict:
    result = read_json(POLICY_PATH)
    if (result.get('schema') != 1 or result.get('qt_version') != '6.12.0'
            or result.get('architecture') != 'arm64'
            or type(result.get('production_integration_allowed')) is not bool):
        raise RuntimeError('Unsupported overlay policy')
    for asset in (result['resource'], result['source_resource']):
        if (not re.fullmatch('[0-9a-f]{64}', asset['sha256']) or not isinstance(asset['bytes'], int)
                or asset['bytes'] <= 0 or Path(asset['name']).name != asset['name']):
            raise RuntimeError('Overlay policy requires actual, complete asset identities')
    if result['resource']['url'] != ('https://github.com/eritpchy/video-subtitle-extractor/releases/download/2.3.1/' + result['resource']['name']):
        raise RuntimeError('Unexpected public overlay release anchor')
    return result


def check_file(path: Path, digest: str, size: int | None = None) -> None:
    if (path.is_symlink() or not path.is_file() or sha256(path) != digest
            or (size is not None and path.stat().st_size != size)):
        raise RuntimeError(f'File identity differs from the reviewed overlay: {path.name}')


def _host(policy: dict) -> None:
    if sys.platform != 'darwin' or platform.machine() != policy['architecture']:
        raise RuntimeError('This overlay only supports native macOS arm64, not Rosetta')
    actual = tuple(int(x) for x in platform.mac_ver()[0].split('.'))
    required = tuple(int(x) for x in policy['minimum_macos_version'].split('.'))
    if actual < required:
        raise RuntimeError(f"This exact Qt runtime declares macOS {policy['minimum_macos_version']} or newer")


def _admission(policy: dict, candidate_testing: bool) -> None:
    if not policy['production_integration_allowed'] and not candidate_testing:
        raise RuntimeError('Overlay admission is still blocked; only explicit candidate testing is permitted')


def _secure_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or parsed.hostname not in ('github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')):
        raise RuntimeError('Overlay download must use its pinned HTTPS GitHub resource')


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _secure_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_archive(destination: Path, *, policy: dict | None = None) -> Path:
    """Fetch only the eventual public release asset, never Actions or Library."""
    policy = policy or load_policy()
    _admission(policy, False)
    asset = policy['resource']
    _secure_url(asset['url'])
    if destination.exists() or destination.is_symlink():
        raise RuntimeError('Download destination must be new')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.vse-qt-download-', delete=False) as file:
        temporary = Path(file.name)
    try:
        opener = urllib.request.build_opener(_Redirect())
        with opener.open(asset['url'], timeout=60) as source, temporary.open('wb') as output:
            count = 0
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > asset['bytes']:
                    raise RuntimeError('Overlay download exceeded its pinned size')
                output.write(block)
        check_file(temporary, asset['sha256'], asset['bytes'])
        # Link, rather than replace, so a newly-created destination cannot be
        # silently overwritten while the download is in progress.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


@dataclass(frozen=True)
class VerifiedResource:
    root: Path
    archive: Path
    policy: dict


def _validate_resource(resource: VerifiedResource) -> dict:
    policy, root = resource.policy, resource.root
    asset = policy['resource']
    check_file(resource.archive, asset['sha256'], asset['bytes'])
    manifest = read_json(root / 'resource-manifest.json')
    check_file(root / 'resource-manifest.json', policy['resource_manifest_sha256'])
    files = archives.inventory(root)
    files.pop('resource-manifest.json', None)
    if manifest.get('files') != files:
        raise RuntimeError('Overlay resource inventory differs')
    if (manifest.get('schema') != 1
            or manifest.get('asset_kind') != 'vse-qt-cocoa-overlay-resource'
            or manifest.get('qt_version') != policy['qt_version']
            or manifest.get('arch') != policy['architecture']
            or manifest.get('source_asset') != policy['source_resource']):
        raise RuntimeError('Overlay resource identity or corresponding source differs')
    plugin = root / 'libqcocoa.dylib'
    check_file(plugin, policy['plugin']['sha256'], policy['plugin']['bytes'])
    metadata = macho_arm64(plugin)
    validate_link_contract(metadata, {})
    if metadata.get('uuid') != policy['plugin']['uuid']:
        raise RuntimeError('Overlay Mach-O UUID differs')
    candidate = read_json(root / 'candidate.json')
    check_file(root / 'candidate.json', policy['candidate_manifest_sha256'])
    if (candidate.get('candidate_sha256') != policy['plugin']['sha256']
            or candidate.get('diagnostic_only') is not True
            or candidate.get('production_integration_allowed') is not False
            or candidate.get('pins') != policy['upstream_pins']):
        raise RuntimeError('Historical candidate source/diagnostic evidence changed')
    for name in ('LGPL-3.0-only.txt', 'GPL-3.0-only.txt', 'BSD-3-Clause.txt'):
        if (root / 'LICENSES' / name).stat().st_size < 100:
            raise RuntimeError('Required Qt license is missing')
    for source, digest in policy['upstream_pins']['patched_sources'].items():
        check_file(root / 'cocoa-modified-source' / Path(source).name, digest)
    return manifest


def prepare_resource(archive: Path, cache_dir: Path, *, policy: dict | None = None) -> VerifiedResource:
    policy = json.loads(json.dumps(policy or load_policy()))
    check_file(archive, policy['resource']['sha256'], policy['resource']['bytes'])
    root = archives.extract_archive(archive, cache_dir, max_entries=2000,
                                    max_uncompressed_bytes=32 * 1024 * 1024)
    if root.name != policy['resource']['root']:
        raise RuntimeError('Unexpected overlay resource root')
    result = VerifiedResource(root, archive.resolve(), policy)
    _validate_resource(result)
    return result


def _owned_file(path: Path, parent: Path) -> Path:
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(parent):
        raise RuntimeError(f'Overlay target is not a regular file inside the allowed environment: {path}')
    if hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise RuntimeError('Overlay may only modify files owned by the current user')
    return path


def inspect_target(purelib: Path, *, policy: dict | None = None) -> dict:
    """No Qt imports: inspect package metadata and exact framework bytes first."""
    policy = policy or load_policy()
    purelib = purelib.resolve(strict=True)
    versions = {}
    for distribution in importlib.metadata.distributions(path=[str(purelib)]):
        name = distribution.metadata.get('Name', '').lower().replace('_', '-')
        if name in ('pyside6', 'pyside6-essentials', 'pyside6-addons', 'shiboken6'):
            if name in versions:
                raise RuntimeError('Duplicate Qt distribution metadata')
            if not distribution.read_text('RECORD'):
                raise RuntimeError('Original Qt/Shiboken wheel RECORD is missing')
            versions[name] = distribution.version
    if (not {'pyside6-essentials', 'shiboken6'}.issubset(versions)
            or any(value != policy['qt_version'] for value in versions.values())):
        raise RuntimeError('Install the exact reviewed PySide6/Shiboken versions before preparing Qt')
    plugin = _owned_file(purelib / PLUGIN, purelib)
    actual = sha256(plugin)
    if actual not in (policy['original_cocoa_sha256'], policy['plugin']['sha256']):
        raise RuntimeError('Refusing to replace an unknown or user-modified Cocoa plugin')
    for name, digest in policy['official_frameworks'].items():
        path = purelib / f'PySide6/Qt/lib/{name}.framework/Versions/A/{name}'
        _owned_file(path, purelib)
        check_file(path, digest)
    for relative, digest in policy['abi_files'].items():
        path = _owned_file(purelib / relative, purelib)
        check_file(path, digest)
    records = {p.relative_to(purelib).as_posix(): sha256(_owned_file(p, purelib))
               for p in purelib.glob('*.dist-info/RECORD')}
    if not any(p.startswith('pyside6_essentials-') for p in records):
        raise RuntimeError('Original official wheel RECORD is missing')
    return {'purelib': str(purelib), 'versions': versions, 'plugin_sha256': actual,
            'official_frameworks': policy['official_frameworks'],
            'abi_files': policy['abi_files'], 'original_RECORD': records}


def _atomic_json(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _apply(purelib: Path, receipt_path: Path, resource: VerifiedResource, *, context: str) -> dict:
    policy = resource.policy
    _validate_resource(resource)
    before = inspect_target(purelib, policy=policy)
    plugin = purelib / PLUGIN
    if before['plugin_sha256'] == policy['plugin']['sha256']:
        if not receipt_path.is_file():
            raise RuntimeError('A patched plugin without its own preparation receipt is not adopted')
        receipt = read_json(receipt_path)
        verify_overlay(purelib, receipt, policy=policy)
        if receipt.get('context') != context:
            raise RuntimeError('An overlay receipt from another preparation context is not adopted')
        return receipt
    if receipt_path.exists() or receipt_path.is_symlink():
        raise RuntimeError('Existing preparation receipt does not match this original runtime')
    temporary = backup = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(dir=plugin.parent, delete=False) as output:
            temporary = Path(output.name)
            with (resource.root / 'libqcocoa.dylib').open('rb') as source:
                shutil.copyfileobj(source, output)
        check_file(temporary, policy['plugin']['sha256'], policy['plugin']['bytes'])
        temporary.chmod(stat.S_IMODE(plugin.stat().st_mode))
        # Keep a same-filesystem hard link to the original inode before the
        # replace. Rollback then requires no new allocation of original bytes,
        # including when a later receipt write fails because the disk is full.
        descriptor, name = tempfile.mkstemp(prefix='.vse-qt-original-', dir=plugin.parent)
        os.close(descriptor)
        backup = Path(name)
        backup.unlink()
        os.link(plugin, backup)
        os.replace(temporary, plugin)
        replaced = True
        after = inspect_target(purelib, policy=policy)
        if after['original_RECORD'] != before['original_RECORD']:
            raise RuntimeError('Preparation changed original vendor RECORD files')
        receipt = {'schema': 1, 'status': 'exact-overlay-applied', 'context': context,
                   'qt_version': policy['qt_version'], 'architecture': policy['architecture'],
                   'resource_sha256': policy['resource']['sha256'],
                   'source_resource': policy['source_resource'],
                   'original_cocoa_sha256': policy['original_cocoa_sha256'],
                   'plugin_sha256': policy['plugin']['sha256'], 'purelib': str(purelib),
                   'official_frameworks': policy['official_frameworks'],
                   'abi_files': policy['abi_files'],
                   'original_RECORD': before['original_RECORD'],
                   'minimum_macos_version': policy['minimum_macos_version'],
                   'production_integration_allowed': False,
                   'external_ax_client_tested': False, 'final_application_acceptance_passed': False,
                   'scope': RECEIPT_SCOPE}
        _atomic_json(receipt_path, receipt)
        return receipt
    except BaseException:
        # Restore only the single exact original file touched by this operation.
        if replaced and backup is not None:
            try:
                os.replace(backup, plugin)
            except OSError as failure:
                preserved = backup
                backup = None  # Never delete the only remaining recovery copy.
                raise RuntimeError('Preparation and rollback failed; do not launch Qt. '
                                   f'Exact original plugin preserved at {preserved}') from failure
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)


def verify_overlay(purelib: Path, receipt: dict, *, policy: dict | None = None) -> dict:
    policy = policy or load_policy()
    actual = inspect_target(purelib, policy=policy)
    if (receipt.get('schema') != 1 or receipt.get('status') != 'exact-overlay-applied'
            or receipt.get('plugin_sha256') != policy['plugin']['sha256']
            or actual['plugin_sha256'] != policy['plugin']['sha256']
            or receipt.get('resource_sha256') != policy['resource']['sha256']
            or receipt.get('source_resource') != policy['source_resource']
            or receipt.get('original_cocoa_sha256') != policy['original_cocoa_sha256']
            or receipt.get('qt_version') != policy['qt_version']
            or receipt.get('architecture') != policy['architecture']
            or receipt.get('minimum_macos_version') != policy['minimum_macos_version']
            or receipt.get('context') not in ('explicit-user-venv', 'candidate-package-stage')
            or receipt.get('scope') != RECEIPT_SCOPE
            or any(receipt.get(flag) is not False for flag in (
                'production_integration_allowed', 'external_ax_client_tested', 'final_application_acceptance_passed'))
            or receipt.get('official_frameworks') != actual['official_frameworks']
            or receipt.get('abi_files') != actual['abi_files']
            or receipt.get('original_RECORD') != actual['original_RECORD']):
        raise RuntimeError('Qt overlay receipt does not match this exact runtime')
    return actual


def stage_notices(stage: Path, resource: VerifiedResource, source_archive: Path) -> dict:
    """Ship complete matching source and immutable historical notices, no binaries changed."""
    _validate_resource(resource)
    policy = resource.policy
    source = policy['source_resource']
    check_file(source_archive, source['sha256'], source['bytes'])
    stage = stage.resolve(strict=True)
    if (stage / 'bundle-manifest.json').exists():
        raise RuntimeError('Notices must be included before final package sealing')
    target = stage / 'third-party/qt-cocoa-ax-overlay'
    if target.exists() or target.is_symlink():
        raise RuntimeError('Overlay notice destination must be fresh')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(stage):
        raise RuntimeError('Overlay notices escape package staging')
    shutil.copytree(resource.root, target)
    # The already-verified small resource is retained byte-for-byte as a
    # rebuild/install input; the separately installed vendor plugin is the
    # only vendor-file modification. No second loose runtime image is shipped.
    (target / 'libqcocoa.dylib').unlink()
    (target / 'resource-manifest.json').rename(target / 'original-resource-manifest.json')
    shutil.copy2(resource.archive, target / policy['resource']['name'])
    shutil.copy2(source_archive, target / source['name'])
    _atomic_json(target / 'overlay-policy.json', policy)
    result = {'directory': target.relative_to(stage).as_posix(),
              'files': archives.inventory(target), 'source_sha256': source['sha256'],
              'historical_candidate_flags_unchanged': True}
    return result


def _venv_notices(prefix: Path, resource: VerifiedResource) -> Path:
    """Retain modification/license/rebuild materials after temporary extraction ends."""
    target = prefix / 'share/vse-qt-ax-overlay'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(prefix):
        raise RuntimeError('Venv notices escape their environment')
    source_notice = ('Complete corresponding source (exact pinned bytes):\n' + resource.policy['source_url'] + '\n'
                     + json.dumps(resource.policy['source_resource'], indent=2) + '\n'
                     'Prepublication candidate testing does not claim this URL is already public.\n')
    expected = archives.inventory(resource.root)
    expected.pop('libqcocoa.dylib')
    expected['original-resource-manifest.json'] = expected.pop('resource-manifest.json')
    expected[resource.policy['resource']['name']] = resource.policy['resource']['sha256']
    expected['CORRESPONDING-SOURCE.txt'] = hashlib.sha256(source_notice.encode()).hexdigest()
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise RuntimeError('Unsafe existing overlay notice directory')
        actual = archives.inventory(target)
        actual.pop('notice-inventory.json', None)
        if actual != expected or read_json(target / 'notice-inventory.json') != expected:
            raise RuntimeError('Existing venv notices differ from the exact reviewed resource')
        return target
    with tempfile.TemporaryDirectory(prefix='.vse-qt-notices-', dir=target.parent) as directory:
        staging = Path(directory) / 'notice'
        shutil.copytree(resource.root, staging)
        (staging / 'libqcocoa.dylib').unlink()
        (staging / 'resource-manifest.json').rename(staging / 'original-resource-manifest.json')
        shutil.copy2(resource.archive, staging / resource.policy['resource']['name'])
        (staging / 'CORRESPONDING-SOURCE.txt').write_text(source_notice, encoding='utf-8')
        _atomic_json(staging / 'notice-inventory.json', archives.inventory(staging))
        staging.rename(target)
    return target


def inspect_current_venv() -> tuple[Path, Path]:
    import sysconfig
    prefix = Path(sys.prefix).resolve(strict=True)
    if prefix == Path(sys.base_prefix).resolve() or not (prefix / 'pyvenv.cfg').is_file():
        raise RuntimeError('Run this command with your own venv Python; system Python is never modified')
    settings = {}
    for line in (prefix / 'pyvenv.cfg').read_text(encoding='utf-8').splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            settings[key.strip().lower()] = value.strip().lower()
    if settings.get('include-system-site-packages') != 'false':
        raise RuntimeError('The venv must disable system-site-packages')
    purelib = Path(sysconfig.get_path('purelib')).resolve(strict=True)
    if not purelib.is_relative_to(prefix):
        raise RuntimeError('The current Python package directory escapes its venv')
    return prefix, purelib


def apply_to_venv(resource: VerifiedResource, *, candidate_testing: bool = False) -> dict:
    _host(resource.policy)
    _admission(resource.policy, candidate_testing)
    prefix, purelib = inspect_current_venv()
    if any(name == 'PySide6' or name.startswith('PySide6.') for name in sys.modules):
        raise RuntimeError('Prepare Qt before importing it; close all GUI processes using this venv')
    _validate_resource(resource)
    inspect_target(purelib, policy=resource.policy)
    _venv_notices(prefix, resource)
    return _apply(purelib, purelib / RECEIPT, resource, context='explicit-user-venv')


def apply_to_stage(stage: Path, python: Path, resource: VerifiedResource, *, candidate_testing: bool = False) -> dict:
    _host(resource.policy)
    _admission(resource.policy, candidate_testing)
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and os.environ.get('RUNNER_OS') == 'macOS'):
        raise RuntimeError('Package staging is restricted to hosted macOS CI')
    stage = stage.resolve(strict=True)
    runtime = (stage / 'runtime').resolve(strict=True)
    if not runtime.is_relative_to(stage) or not python.resolve(strict=True).is_relative_to(runtime):
        raise RuntimeError('Packaged interpreter must belong to the temporary staged runtime')
    if (stage / 'bundle-manifest.json').exists():
        raise RuntimeError('Never modify an already sealed application; build a new staging tree')
    query = 'import json,sysconfig; print(json.dumps(sysconfig.get_path("purelib")))'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'DYLD_', 'QT_'))}
    purelib = Path(json.loads(subprocess.check_output([str(python), '-I', '-B', '-c', query], env=env, text=True))).resolve(strict=True)
    if not purelib.is_relative_to(runtime):
        raise RuntimeError('Packaged Python resolves packages outside its runtime')
    return _apply(purelib, runtime / RECEIPT, resource, context='candidate-package-stage')
