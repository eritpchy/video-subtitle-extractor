"""Synthetic package integrity checks, not actual Qt/AX execution."""
import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from scripts import package_macos_qt_overlay as package
from scripts import macos_qt_overlay as overlay
from test_macos_qt_overlay import resource, purelib  # Reuse strict synthetic Mach-O fixture.


@pytest.fixture
def staged(tmp_path, resource, purelib, monkeypatch):
    stage = tmp_path / 'stage'
    runtime_lib = stage / 'runtime/lib/python3.12/site-packages'
    shutil.copytree(purelib, runtime_lib)
    retained = [{'path': p.relative_to(runtime_lib).as_posix(), 'size': p.stat().st_size,
                 'sha256': overlay.sha256(p)} for p in sorted(runtime_lib.rglob('*')) if p.is_file()]
    before = {'schema': 1, 'engine': 'native', 'removed': [], 'retained': retained,
              'retained_bytes': sum(x['size'] for x in retained), 'removed_bytes': 0,
              'original_RECORD_unchanged': True}
    prune = stage / 'runtime/inference-prune.json'
    prune.write_text(json.dumps(before))
    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(json.dumps(resource.policy))
    monkeypatch.setattr(overlay, 'POLICY_PATH', policy_file)
    monkeypatch.setattr(overlay, 'load_policy', lambda: copy.deepcopy(resource.policy))
    monkeypatch.setattr(overlay, 'prepare_resource', lambda archive, cache: resource)
    monkeypatch.setattr(overlay, 'apply_to_stage', lambda stage, python, resource, candidate_testing:
                        overlay._apply(runtime_lib, stage / 'runtime' / overlay.RECEIPT,
                                       resource, context='candidate-package-stage'))
    source = tmp_path / 'source.tar.xz'
    source.write_bytes(b'source')
    metadata = package.apply_candidate(stage, stage / 'runtime/bin/python3.12', resource.archive, source)
    manifest = {'platform': 'macos', 'arch': 'arm64', 'build_profile': 'native-coreml', 'qt_ax_overlay': metadata}
    return stage, manifest, before


def test_only_plugin_prune_entry_changes_and_original_source_notices_survive(staged):
    stage, manifest, before = staged
    after = overlay.read_json(stage / 'runtime/inference-prune.json')
    old = {x['path']: x for x in before['retained']}
    new = {x['path']: x for x in after['retained']}
    assert set(old) == set(new)
    assert [key for key in old if old[key] != new[key]] == [overlay.PLUGIN.as_posix()]
    assert after['retained_bytes'] == sum(x['size'] for x in after['retained'])
    assert overlay.read_json(stage / manifest['qt_ax_overlay']['original_prune']) == before
    result = package.verify_candidate(stage, manifest)
    assert result['overlay_integrity'] == 'passed'
    assert result['production_integration_allowed'] is False
    assert result['external_ax_client_tested'] is False
    assert not (stage / manifest['qt_ax_overlay']['notices'] / 'libqcocoa.dylib').exists()


@pytest.mark.parametrize('target', ['plugin', 'framework', 'record', 'extra', 'prune', 'original', 'policy', 'source', 'resource'])
def test_extracted_package_any_mutation_fails(staged, target):
    stage, manifest, before = staged
    pure = stage / 'runtime/lib/python3.12/site-packages'
    notice = stage / manifest['qt_ax_overlay']['notices']
    policy = overlay.load_policy()
    path = {'plugin': pure / overlay.PLUGIN, 'framework': pure / 'PySide6/Qt/lib/QtCore.framework/Versions/A/QtCore',
            'record': pure / 'shiboken6-6.12.0.dist-info/RECORD', 'extra': pure / 'unreviewed.txt',
            'prune': stage / 'runtime/inference-prune.json', 'original': stage / manifest['qt_ax_overlay']['original_prune'],
            'policy': stage / 'native/qt/macos-qt-overlay.json',
            'source': notice / policy['source_resource']['name'], 'resource': notice / policy['resource']['name']}[target]
    path.write_bytes(b'changed')
    with pytest.raises((RuntimeError, ValueError)):
        package.verify_candidate(stage, manifest)


@pytest.mark.parametrize('mutation', ['no-overlay', 'prod', 'ax', 'acceptance', 'source-context', 'wrong-platform', 'path-escape', 'extra-prune-change'])
def test_no_promotion_foreign_context_or_broadened_prune(staged, mutation):
    stage, manifest, before = staged
    metadata = manifest['qt_ax_overlay']
    if mutation == 'no-overlay':
        manifest.pop('qt_ax_overlay')
    elif mutation in ('prod', 'ax', 'acceptance'):
        metadata[{'prod': 'production_integration_allowed', 'ax': 'external_ax_client_tested', 'acceptance': 'final_application_acceptance_passed'}[mutation]] = True
    elif mutation == 'wrong-platform':
        manifest['platform'] = 'linux'
    elif mutation == 'path-escape':
        metadata['receipt'] = '../foreign.json'
    elif mutation == 'source-context':
        p = stage / metadata['receipt']; r = overlay.read_json(p)
        r['context'] = 'explicit-user-venv'; p.write_text(json.dumps(r)); metadata['receipt_sha256'] = overlay.sha256(p)
    else:
        p = stage / 'runtime/inference-prune.json'; r = overlay.read_json(p)
        r['unrelated_new_field'] = True; p.write_text(json.dumps(r)); metadata['rebound_prune_sha256'] = overlay.sha256(p)
    with pytest.raises((RuntimeError, ValueError)):
        package.verify_candidate(stage, manifest)


def test_package_hook_precedes_floor_manifest_and_never_implicitly_patches():
    root = Path(__file__).resolve().parents[1]
    code = (root / 'scripts/package_release.py').read_text()
    assert code.index('qt_overlay_info = apply_candidate') < code.index('deployment = portable_launchers') < code.index('"qt_ax_overlay": qt_overlay_info')
    assert 'Mac CoreML candidate requires the explicit reviewed Qt overlay resources' in code
    assert 'verify_candidate(root, manifest)' in code


def test_direct_script_help_uses_only_trusted_repo_and_stdlib(tmp_path):
    import subprocess, sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-I', '-S', str(root / 'scripts/package_macos_qt_overlay.py'), '--help'],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--archives' in result.stdout


@pytest.mark.parametrize('platform,arch,profile,accelerator,options', [
    ('macos','arm64','native','coreml',{}),
    ('macos','arm64','native','coreml',{'qt_overlay_archive': Path('one.zip')}),
    ('macos','arm64','native','cpu',{'qt_overlay_candidate_testing': True}),
    ('windows','x64','native','cpu',{'qt_overlay_candidate_testing': True}),
    ('linux','x64','paddle','cpu',{'qt_overlay_candidate_testing': True}),
])
def test_packager_never_implicitly_applies_or_silently_omits_overlay(monkeypatch, tmp_path, platform, arch, profile, accelerator, options):
    from scripts import package_release
    monkeypatch.setattr(package_release, 'host_platform', lambda: (platform, arch))
    # Rejection must happen before downloads, git operations or any package mutation.
    monkeypatch.setattr(package_release.subprocess, 'check_output', lambda *a, **k: pytest.fail('unexpected build operation'))
    with pytest.raises(ValueError, match='Qt overlay|Mac CoreML candidate'):
        package_release.create_bundle(tmp_path, tmp_path / 'out', profile=profile,
                                      native_ocr=tmp_path / 'native' if profile == 'native' else None,
                                      accelerator=accelerator, **options)
