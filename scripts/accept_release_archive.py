"""Bind completed final-archive checks to candidate acceptance receipts.

This consumes real package/launcher evidence; it runs no workloads, installs
nothing, and never infers GPU acceptance from a CPU/Server CI result. A receipt
is local CI evidence, not an independent signed or hardware attestation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

try:
    from .bundle_bootstrap import sha256
    from .release_guard import required_acceptance_checks, validate_acceptance_receipt
    from .release_profiles import CANDIDATE_TARGETS, PROFILES, archive_name
except ImportError:
    from bundle_bootstrap import sha256
    from release_guard import required_acceptance_checks, validate_acceptance_receipt
    from release_profiles import CANDIDATE_TARGETS, PROFILES, archive_name


SCENARIOS = ('clean-host-path', 'contaminated-host-environment')
MODES = ('fast', 'accurate', 'auto')
ISSUES = frozenset(('multiline', 'av1', 'timeline'))
IDENTITY = ('commit', 'version', 'platform', 'arch', 'build_profile')
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RuntimeError(f'Duplicate JSON field: {name}')
        result[name] = value
    return result


def _decode(data: bytes, label: str, kind=dict):
    try:
        document = json.loads(data, object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as error:
        raise RuntimeError(f'Invalid evidence JSON: {label}') from error
    if not isinstance(document, kind):
        raise RuntimeError(f'Wrong evidence structure: {label}')
    return document


def _root(directory: Path) -> Path:
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError(f'Missing or unsafe evidence directory: {directory}')
    return directory.resolve(strict=True)


def _file(path: Path, root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RuntimeError(f'Evidence escaped its expected directory: {path}') from error
    if '..' in relative.parts:
        raise RuntimeError(f'Evidence escaped its expected directory: {path}')
    # macOS temporary paths can start at /var, an alias of /private/var.
    # Establish one canonical boundary, while still inspecting each original
    # descendant before resolving it: an in-root symlink is not valid evidence.
    canonical_root = _root(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f'Symlink is not accepted as final evidence: {path}')
    if not path.is_file():
        raise RuntimeError(f'Missing or unsafe final evidence: {path}')
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(canonical_root) or resolved != canonical_root / relative:
        raise RuntimeError(f'Missing or unsafe final evidence: {path}')
    return resolved


class Evidence:
    """Read bounded, safe files once and preserve their exact original hashes."""
    def __init__(self):
        self.index = {}
        self.sources = []

    def read(self, root: Path, relative: str, scope: str, kind=dict):
        # Keep the established canonical root for the later unchanged check;
        # an ancestor alias changing after this read must not redirect it.
        root = _root(root)
        path = _file(root / relative, root)
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise RuntimeError(f'Oversized final evidence: {path}')
        data = path.read_bytes()
        key = f'{scope}/{relative}'
        record = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        if key in self.index and self.index[key] != record:
            raise RuntimeError(f'Conflicting final evidence: {key}')
        self.index[key] = record
        self.sources.append((path, root, record))
        return _decode(data, key, kind)

    def assert_unchanged(self):
        for path, root, record in self.sources:
            _file(path, root)
            if path.stat().st_size != record['bytes'] or sha256(path) != record['sha256']:
                raise RuntimeError(f'Final evidence changed during acceptance: {path}')


def _passed_audit(document, label: str):
    if not isinstance(document, dict) or document.get('passed') is not True or document.get('errors') != []:
        raise RuntimeError(f'Final dependency isolation did not pass: {label}')


def _mode_report(document: dict, mode: str, label: str):
    command = document.get('command')
    if (document.get('result') != 'passed' or document.get('mode') != mode
            or document.get('cpu_forced') is not True or not isinstance(command, list)
            or not command or any(not isinstance(part, str) for part in command)
            or command.count('--cpu') != 1 or command.count('--mode') != 1
            or any(part.startswith(('--mode=', '--cpu=')) for part in command)):
        raise RuntimeError(f'Missing genuine explicit CPU {mode} video acceptance: {label}')
    index = command.index('--mode')
    if index + 1 >= len(command) or command[index + 1] != mode:
        raise RuntimeError(f'CLI mode does not match {mode} acceptance: {label}')


def _workload_evidence(evidence: Evidence, root: Path, relative: str, scope: str):
    prefix = relative + '/' if relative else ''
    for mode in MODES:
        directory = 'bundle-e2e' if mode == 'fast' else f'bundle-e2e-{mode}'
        name = prefix + directory + '/report.json'
        _mode_report(evidence.read(root, name, scope), mode, f'{scope}/{name}')
    name = prefix + 'bundle-issues/report.json'
    issues = evidence.read(root, name, scope, list)
    if (len(issues) != len(ISSUES) or any(not isinstance(row, dict) or not isinstance(row.get('case'), str) for row in issues)
            or {row.get('case') for row in issues} != ISSUES
            or any(row.get('result') != 'passed' for row in issues)):
        raise RuntimeError(f'Multiline/AV1/timeline final acceptance is missing or failed: {scope}/{name}')
    name = prefix + 'dependency-isolation/summary.json'
    _passed_audit(evidence.read(root, name, scope), f'{scope}/{name}')


def read_manifest(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        manifests = [name for name in names if name.endswith('/bundle-manifest.json')]
        if len(names) != len(set(names)) or len(manifests) != 1:
            raise RuntimeError(f'Archive requires unique entries and one manifest: {archive.name}')
        entry = bundle.getinfo(manifests[0])
        if entry.file_size > 16 * 1024 * 1024:
            raise RuntimeError(f'Oversized bundle manifest: {archive.name}')
        manifest = _decode(bundle.read(entry), archive.name)
    if any(not isinstance(manifest.get(key), str) for key in IDENTITY):
        raise RuntimeError(f'Missing final bundle identity: {archive.name}')
    if not re.fullmatch(r'[a-f0-9]{40}', manifest['commit']):
        raise RuntimeError(f'Invalid final bundle commit: {archive.name}')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', manifest['version']):
        raise RuntimeError(f'Invalid final bundle version: {archive.name}')
    target = (manifest['platform'], manifest['arch'], manifest['build_profile'])
    if target not in CANDIDATE_TARGETS:
        raise RuntimeError(f'Unknown or blocked final bundle profile: {archive.name}')
    profile = PROFILES[manifest['build_profile']]
    if (archive.name != archive_name(manifest['version'], *target)
            or manifest.get('ocr_backend') != profile['backend']
            or manifest.get('accelerator') != profile['accelerator']
            or manifest.get('format') != 2 or manifest.get('python') != '3.12'):
        raise RuntimeError(f'Final bundle filename/backend/runtime identity mismatch: {archive.name}')
    expected = archive.stem + ('/Video Subtitle Extractor.app/Contents/Resources' if manifest['platform'] == 'macos' else '') + '/bundle-manifest.json'
    if manifests != [expected]:
        raise RuntimeError(f'Unexpected final manifest location: {archive.name}')
    return manifest


def _bound_report(document: dict, expected: dict, label: str):
    if any(document.get(key) != value or type(document.get(key)) is not type(value)
           for key, value in expected.items()):
        raise RuntimeError(f'Evidence is not a passing result for this exact archive: {label}')


def prepare_acceptance(archive: Path, archives: Path, portable: Path) -> tuple[dict, Evidence]:
    _file(archive, archives)
    digest = sha256(archive)
    checksum = _file(archive.with_suffix('.zip.sha256'), archives)
    if checksum.read_text(encoding='utf-8').strip() != f'{digest}  {archive.name}':
        raise RuntimeError(f'Final archive checksum/name mismatch: {archive.name}')
    manifest = read_manifest(archive)
    identity = {key: manifest[key] for key in IDENTITY}
    evidence = Evidence()
    smoke_relative = f'test-results/{archive.stem}'
    package = evidence.read(archives, smoke_relative + '/package-verification.json', 'package')
    _bound_report(package, {'schema': 1, 'result': 'passed', 'archive': archive.name,
                           'archive_sha256': digest, **identity,
                           'payload': 'passed', 'launcher_smoke': 'passed'}, 'package-verification.json')
    _workload_evidence(evidence, archives, smoke_relative, 'package')
    if manifest['ocr_backend'] == 'paddle':
        try:
            from .check_paddle_models import validate_report
        except ImportError:
            from check_paddle_models import validate_report
        # This is original Paddle inference in the final extracted bundle,
        # not Native parity, a source-tree test, or mere model-file presence.
        model_report = evidence.read(archives, smoke_relative + '/paddle-models/report.json', 'package')
        try:
            validate_report(model_report, identity={key: manifest[key]
                                                   for key in ('commit', 'version', 'build_profile')})
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError(f'Final Paddle model acceptance did not pass: {error}') from error
    portable_relative = archive.stem
    environment = evidence.read(portable, portable_relative + '/portable-environment-report.json', 'portable')
    _bound_report(environment, {'result': 'passed', 'archive_sha256': digest,
                               'commit': manifest['commit'], 'platform': manifest['platform'],
                               'arch': manifest['arch'], 'profile': manifest['ocr_backend']},
                  'portable-environment-report.json')
    scenarios = environment.get('scenarios')
    if (not isinstance(scenarios, list) or len(scenarios) != len(SCENARIOS)
            or any(not isinstance(case, dict) or not isinstance(case.get('scenario'), str) for case in scenarios)
            or {case['scenario'] for case in scenarios} != set(SCENARIOS)):
        raise RuntimeError('Both unique portable environment scenarios are required')
    for case in scenarios:
        scenario = case['scenario']
        _bound_report(case, {'result': 'passed'}, scenario)
        _passed_audit(case.get('audit'), scenario)
        case_relative = portable_relative + '/' + scenario
        detail = evidence.read(portable, case_relative + '/scenario-report.json', 'portable')
        _bound_report(detail, {'scenario': scenario, 'result': 'passed'}, scenario + '/scenario-report.json')
        _passed_audit(detail.get('audit'), scenario + '/scenario-report.json')
        _workload_evidence(evidence, portable, case_relative, 'portable')
    receipt = {'schema': 1, 'archive': archive.name, 'archive_sha256': digest, **identity,
               'checks': {check: 'passed' for check in required_acceptance_checks(manifest['build_profile'])},
               'gpu_inference_verified': False, 'gpu_validation': 'not-run-server-ci',
               'evidence': evidence.index,
               'scope': 'Final ZIP launcher, explicit CPU video modes, regressions and child-environment isolation; not GPU certification.'}
    return receipt, evidence


def _remove_receipts(archives: list[Path]):
    for archive in archives:
        receipt = archive.with_suffix('.zip.acceptance.json')
        # A dangling link is removed as a link, never followed. A directory is
        # an error, not permission to recursively delete unrelated content.
        if receipt.is_dir() and not receipt.is_symlink():
            raise RuntimeError(f'Acceptance output is a directory: {receipt}')
        receipt.unlink(missing_ok=True)


def accept_archives(archive_directory: Path, portable_directory: Path) -> list[Path]:
    archives_root = _root(archive_directory)
    archives = sorted(archives_root.glob('*.zip'))
    if not archives:
        raise RuntimeError('No final ZIPs found; acceptance cannot be skipped')
    _remove_receipts(archives)
    # Remove old passes even if an evidence root is now missing or broken.
    portable_root = _root(portable_directory)
    pending = []
    try:
        for archive in archives:
            receipt, evidence = prepare_acceptance(archive, archives_root, portable_root)
            pending.append((archive, receipt, evidence))
        # Validate all candidates before writing any successful receipt.
        for archive, receipt, evidence in pending:
            evidence.assert_unchanged()
            if sha256(_file(archive, archives_root)) != receipt['archive_sha256']:
                raise RuntimeError(f'Final archive changed during acceptance: {archive.name}')
        outputs = []
        for archive, receipt, evidence in pending:
            output = archive.with_suffix('.zip.acceptance.json')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                                 prefix=output.name + '.', suffix='.tmp',
                                                 dir=archives_root, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump(receipt, stream, indent=2, ensure_ascii=False)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, output)
                validate_acceptance_receipt(output, archive, receipt['archive_sha256'], receipt)
                outputs.append(output)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return outputs
    except BaseException:
        _remove_receipts(archives)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--portable-evidence', type=Path, required=True)
    args = parser.parse_args(argv)
    outputs = accept_archives(args.archives, args.portable_evidence)
    print(json.dumps({'result': 'passed', 'receipts': list(map(str, outputs)),
                      'gpu_inference_verified': False}, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        raise SystemExit(f'Final archive acceptance blocked: {error}')
