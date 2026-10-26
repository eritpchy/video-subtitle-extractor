"""Candidate-only single-plugin integration; never release admission."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import macos_qt_overlay as overlay
from scripts import prune_inference_runtime as pruning


def rebind_prune(before: dict, purelib: Path, receipt: dict) -> dict:
    """Rebind exactly one reviewed Cocoa file, retaining every other record."""
    policy = overlay.load_policy()
    overlay.validate_receipt_identity(receipt)
    if receipt.get('context') != 'candidate-package-stage':
        raise RuntimeError('Package pruning requires its own candidate-stage receipt')
    relative = overlay.PLUGIN.as_posix()
    rows = [row for row in before.get('retained', []) if row.get('path') == relative]
    if len(rows) != 1 or rows[0].get('sha256') != policy['original_cocoa_sha256']:
        raise RuntimeError('Original pruning record must bind the official Cocoa plugin')
    if (before.get('retained_bytes') != sum(row['size'] for row in before['retained'])
            or len({row['path'] for row in before['retained']}) != len(before['retained'])):
        raise RuntimeError('Original pruning inventory has invalid totals or duplicate paths')
    plugin = purelib / overlay.PLUGIN
    overlay.check_file(plugin, policy['plugin']['sha256'], policy['plugin']['bytes'])
    result = copy.deepcopy(before)
    new = next(row for row in result['retained'] if row['path'] == relative)
    new.update(size=plugin.stat().st_size, sha256=overlay.sha256(plugin))
    result['retained_bytes'] = sum(row['size'] for row in result['retained'])
    pruning.verify(purelib, result)  # Other changed/missing/extra files still fail.
    return result


def apply_candidate(stage: Path, python: Path, archive: Path, source_archive: Path) -> dict:
    stage = stage.resolve(strict=True)
    original_path = stage / 'runtime/inference-prune.json'
    original_bytes = original_path.read_bytes()
    before = overlay.read_json(original_path)
    policy = overlay.load_policy()
    # All supported packaged macOS interpreters use this audited 3.12 layout;
    # apply_to_stage independently queries sysconfig and rejects another root.
    purelib = stage / 'runtime/lib/python3.12/site-packages'
    pruning.verify(purelib, before)
    with tempfile.TemporaryDirectory(prefix='vse-qt-resource-') as temporary:
        resource = overlay.prepare_resource(archive, Path(temporary) / 'verified')
        receipt = overlay.apply_to_stage(stage, python, resource, candidate_testing=True)
        if Path(receipt['purelib']).resolve(strict=True) != purelib.resolve(strict=True):
            raise RuntimeError('Qt overlay and pruning resolved different package roots')
        notices = overlay.stage_notices(stage, resource, source_archive)
    notice_dir = stage / notices['directory']
    (notice_dir / 'original-inference-prune.json').write_bytes(original_bytes)
    rebound = rebind_prune(before, purelib, receipt)
    pending = original_path.with_name(original_path.name + '.qt-pending')
    with pending.open('x', encoding='utf-8') as stream:
        json.dump(rebound, stream, indent=2)
        stream.write('\n')
    pending.replace(original_path)
    pruning.verify(purelib, overlay.read_json(original_path))
    overlay.verify_overlay(purelib, receipt)
    policy_path = stage / 'native/qt/macos-qt-overlay.json'
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(overlay.POLICY_PATH, policy_path)
    return {
        'schema': 1, 'candidate_testing': True,
        'production_integration_allowed': False, 'external_ax_client_tested': False,
        'final_application_acceptance_passed': False,
        'receipt': 'runtime/' + overlay.RECEIPT,
        'receipt_sha256': overlay.sha256(stage / 'runtime' / overlay.RECEIPT),
        'policy_sha256': overlay.sha256(policy_path),
        'original_prune': notices['directory'] + '/original-inference-prune.json',
        'original_prune_sha256': overlay.sha256(notice_dir / 'original-inference-prune.json'),
        'rebound_prune_sha256': overlay.sha256(original_path),
        'resource_sha256': policy['resource']['sha256'],
        'source_sha256': policy['source_resource']['sha256'],
        'notices': notices['directory'],
        'scope': 'Explicit candidate preparation; final ZIP checks and external AX remain separate',
    }


def verify_candidate(root: Path, manifest: dict) -> dict:
    """Verify the extracted candidate without executing or trusting build paths."""
    if (manifest.get('platform'), manifest.get('arch'), manifest.get('build_profile')) != ('macos', 'arm64', 'native-coreml'):
        raise RuntimeError('Qt candidate overlay is limited to macOS arm64 Native CoreML')
    metadata = manifest.get('qt_ax_overlay')
    if (not isinstance(metadata, dict) or metadata.get('schema') != 1
            or metadata.get('candidate_testing') is not True
            or any(metadata.get(key) is not False for key in (
                'production_integration_allowed', 'external_ax_client_tested', 'final_application_acceptance_passed'))
            or metadata.get('receipt') != 'runtime/' + overlay.RECEIPT
            or metadata.get('notices') != 'third-party/qt-cocoa-ax-overlay'
            or metadata.get('original_prune') != 'third-party/qt-cocoa-ax-overlay/original-inference-prune.json'):
        raise RuntimeError('Missing or misrepresented Qt candidate preparation')
    policy = overlay.load_policy()
    paths = {
        'receipt_sha256': root / metadata['receipt'],
        'policy_sha256': root / 'native/qt/macos-qt-overlay.json',
        'original_prune_sha256': root / metadata['original_prune'],
        'rebound_prune_sha256': root / 'runtime/inference-prune.json',
    }
    for key, path in paths.items():
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
            raise RuntimeError('Qt candidate metadata escapes the extracted package')
        overlay.check_file(path, metadata[key])
    if overlay.read_json(paths['policy_sha256']) != policy:
        raise RuntimeError('Packaged Qt policy differs from reviewed build policy')
    receipt = overlay.read_json(paths['receipt_sha256'])
    purelib = root / 'runtime/lib/python3.12/site-packages'
    overlay.verify_overlay(purelib, receipt)
    before = overlay.read_json(paths['original_prune_sha256'])
    rebound = rebind_prune(before, purelib, receipt)
    if overlay.read_json(paths['rebound_prune_sha256']) != rebound:
        raise RuntimeError('Qt pruning update changed more than the reviewed Cocoa record')
    notice_dir = root / metadata['notices']
    for key, name in (('resource', 'resource_sha256'), ('source_resource', 'source_sha256')):
        value = policy[key]
        if metadata.get(name) != value['sha256']:
            raise RuntimeError('Qt candidate source/resource identity differs')
        overlay.check_file(notice_dir / value['name'], value['sha256'], value['bytes'])
    return {'overlay_integrity': 'passed', 'production_integration_allowed': False,
            'external_ax_client_tested': False, 'plugin_sha256': receipt['plugin_sha256']}


def verify_archives(directory: Path) -> list[dict]:
    """Require exactly one fully verified Mac CoreML overlay in candidate assets."""
    import zipfile
    from scripts.package_release import extract_checked
    from scripts.bundle_bootstrap import verify_payload
    found = []
    for archive in sorted(directory.glob('*.zip')):
        with zipfile.ZipFile(archive) as bundle:
            manifests = [x for x in bundle.infolist() if x.filename.endswith('/bundle-manifest.json')]
            if len(manifests) != 1 or manifests[0].file_size > 16 * 1024 * 1024:
                raise RuntimeError('Candidate ZIP lacks a unique bounded manifest')
            # Safe extraction below and the normal seven-profile release guard
            # independently validate ZIP paths, identities and final receipts.
            manifest = json.loads(bundle.read(manifests[0]))
        if manifest.get('platform') != 'macos':
            continue
        with tempfile.TemporaryDirectory(prefix='vse-qt-final-') as temporary:
            outer = extract_checked(archive, Path(temporary))
            root = outer / 'Video Subtitle Extractor.app/Contents/Resources'
            manifest = overlay.read_json(root / 'bundle-manifest.json')
            verify_payload(root, manifest)
            found.append(verify_candidate(root, manifest))
    if len(found) != 1:
        raise RuntimeError('Candidate set requires exactly one reviewed Mac CoreML overlay')
    return found


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archives', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_archives(args.archives), indent=2))
