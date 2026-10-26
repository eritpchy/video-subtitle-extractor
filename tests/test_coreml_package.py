"""CoreML package identity is not CPU relabeling or physical GPU certification."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.bundle_bootstrap import configure_native_provider, sha256
from scripts.native_package import stage_native_artifact
from scripts.release_profiles import archive_name, profile_identity, profile_requirements, BLOCKED_PROFILES
from test_native_package import fixture_artifact

ROOT = Path(__file__).resolve().parents[1]


def coreml_artifact(root):
    fixture_artifact(root)
    (root / 'libonnxruntime.so.1.31.0').rename(root / 'libonnxruntime.1.dylib')
    capability = {'schema_version': 1, 'ort_version': '1.31.0',
                  'available_providers': ['CPUExecutionProvider', 'CoreMLExecutionProvider'],
                  'supported_request_providers': ['cpu', 'coreml'], 'default_request_provider': 'cpu',
                  'gpu_execution_verified': False,
                  'coreml': {'available': True, 'model_format': 'MLProgram',
                             'compute_units': 'CPUAndGPU', 'ane_allowed': False,
                             'gpu_execution_verified': False}}
    (root / 'native-capabilities.json').write_text(json.dumps(capability))
    refresh_inventory(root)
    return root


def refresh_inventory(root):
    files = [{'path': p.relative_to(root).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)}
             for p in root.rglob('*') if p.is_file() and p.name != 'size-audit.json']
    provenance = {'native_capabilities': {'path': 'native-capabilities.json',
        'sha256': sha256(root / 'native-capabilities.json'),
        'worker_sha256': sha256(root / 'native-ocr'),
        'runtime_sha256': sha256(root / 'libonnxruntime.1.dylib'),
        'telemetry_disabled_before_exec': True}}
    (root / 'size-audit.json').write_text(json.dumps({'files': files, 'provenance': provenance}))


def test_coreml_identity_uses_same_environment_and_is_not_ready():
    assert profile_identity('native', 'coreml')[0] == 'native-coreml'
    assert profile_requirements(ROOT, 'native-coreml') == profile_requirements(ROOT, 'native-cpu')
    assert archive_name('2.3.0', 'macos', 'arm64', 'native-coreml').endswith('-native-coreml.zip')
    assert 'native-coreml' in BLOCKED_PROFILES  # No real Mac GPU evidence yet.
    for target in [('linux', 'x64'), ('windows', 'x64'), ('macos', 'x64')]:
        with pytest.raises(ValueError, match='macOS arm64'):
            archive_name('2.3.0', *target, 'native-coreml')


def test_stage_coreml_requires_real_probe_and_same_fixed_models(tmp_path):
    source = coreml_artifact(tmp_path / 'source')
    with patch('scripts.native_package.verify_native_model_assets') as verify:
        report = stage_native_artifact(source, tmp_path / 'target', 'macos', provider='coreml')
    verify.assert_called_once_with(source.resolve())
    assert report['profile'] == 'native-coreml'
    assert report['requested_provider'] == 'coreml'
    assert report['gpu_inference_verified'] is False
    assert report['capability']['supported_request_providers'] == ['cpu', 'coreml']
    assert 'native-capabilities.json' in report['files']


@pytest.mark.parametrize('change', ['missing', 'unavailable', 'new-ort', 'ane', 'gpu-claim', 'worker', 'runtime', 'telemetry'])
def test_stage_coreml_rejects_false_capability_or_provenance(tmp_path, change):
    source = coreml_artifact(tmp_path / 'source')
    capability_path = source / 'native-capabilities.json'
    capability = json.loads(capability_path.read_bytes())
    if change in ('unavailable', 'new-ort', 'ane', 'gpu-claim'):
        if change == 'unavailable':
            capability['coreml']['available'] = False
            capability['supported_request_providers'] = ['cpu']
            capability['available_providers'] = ['CPUExecutionProvider']
        elif change == 'new-ort':
            capability['ort_version'] = '999'
        elif change == 'ane':
            capability['coreml']['ane_allowed'] = True
        else:
            capability['coreml']['gpu_execution_verified'] = True
        capability_path.write_text(json.dumps(capability))
        refresh_inventory(source)
    else:
        inventory_path = source / 'size-audit.json'
        inventory = json.loads(inventory_path.read_bytes())
        if change == 'missing':
            inventory['files'] = [row for row in inventory['files'] if row['path'] != capability_path.name]
        else:
            key = {'worker': 'worker_sha256', 'runtime': 'runtime_sha256', 'telemetry': 'telemetry_disabled_before_exec'}[change]
            inventory['provenance']['native_capabilities'][key] = False if change == 'telemetry' else '0' * 64
        inventory_path.write_text(json.dumps(inventory))
    with patch('scripts.native_package.verify_native_model_assets') as verify:
        with pytest.raises(ValueError):
            stage_native_artifact(source, tmp_path / 'target', 'macos', provider='coreml')
    verify.assert_not_called()
    assert not (tmp_path / 'target').exists()


def test_coreml_model_verifier_failure_prevents_any_staging(tmp_path):
    source = coreml_artifact(tmp_path / 'source')
    with patch('scripts.native_package.verify_native_model_assets', side_effect=ValueError('changed fixed models')):
        with pytest.raises(ValueError, match='fixed models'):
            stage_native_artifact(source, tmp_path / 'target', 'macos', provider='coreml')
    assert not (tmp_path / 'target').exists()


def manifest():
    return {'platform': 'macos', 'arch': 'arm64', 'ocr_backend': 'native',
            'accelerator': 'coreml', 'build_profile': 'native-coreml',
            'native_ocr': {'profile': 'native-coreml'}}


@pytest.mark.parametrize('inherited', [None, 'cpu', 'coreml'])
def test_coreml_bundle_pins_acceleration_not_foreign_default(inherited):
    environment = {'ORT_DISABLE_TELEMETRY': '0'}
    if inherited is not None:
        environment['VSE_NATIVE_OCR_PROVIDER'] = inherited
    configure_native_provider(manifest(), environment)
    assert environment == {'VSE_NATIVE_OCR_PROVIDER': 'coreml', 'ORT_DISABLE_TELEMETRY': '1'}
    # Explicit --cpu is consumed by production hardware after this policy;
    # it never requires a second environment/package.


def test_unsupported_provider_is_not_disguised_as_a_successful_fallback():
    environment = {'VSE_NATIVE_OCR_PROVIDER': 'cuda'}
    configure_native_provider(manifest(), environment)
    assert environment['VSE_NATIVE_OCR_PROVIDER'] == 'cuda'


@pytest.mark.parametrize('key,value', [('platform', 'windows'), ('arch', 'x64'),
                                      ('build_profile', 'native-cpu'), ('native_ocr', {'profile': 'native-cpu'})])
def test_coreml_bootstrap_rejects_misidentified_package(key, value):
    item = manifest(); item[key] = value
    with pytest.raises(RuntimeError, match='identity'):
        configure_native_provider(item, {})


def test_native_all_model_launcher_uses_bundled_python_and_external_fixtures(tmp_path):
    from scripts.bundle_bootstrap import run_native_model_acceptance, main
    root = tmp_path / 'final app'
    python = root / 'runtime/python'
    fixtures = tmp_path / 'external frozen inputs'; fixtures.mkdir()
    results = tmp_path / 'results'
    env = {'VSE_OFFLINE': '1', 'VSE_NATIVE_OCR_PROVIDER': 'coreml'}
    with patch('scripts.bundle_bootstrap.subprocess.run') as run:
        run_native_model_acceptance(root, python, fixtures, results, env)
    assert run.call_args.args[0] == [str(python), str(root / 'scripts/check_native_models.py'),
        '--app-root', str(root), '--fixtures', str(fixtures.resolve()), '--provider', 'both',
        '--report', str(results / 'native-models/report.json')]
    assert run.call_args.kwargs == {'cwd': root, 'check': True, 'env': env, 'timeout': 3600}
    forbidden = root / 'fixtures'; forbidden.mkdir(parents=True)
    with patch('scripts.bundle_bootstrap.subprocess.run') as run:
        with pytest.raises(RuntimeError, match='outside the customer package'):
            run_native_model_acceptance(root, python, forbidden, results, env)
    run.assert_not_called()
    with pytest.raises(SystemExit) as error:
        main(['--native-model-fixtures', 'not-opened'])
    assert error.value.code == 2


def test_native_copy_mutation_is_not_reinventoried_as_trusted(tmp_path):
    import shutil
    source = coreml_artifact(tmp_path / 'source')
    copy2 = shutil.copy2
    def corrupted_copy(origin, target):
        copy2(origin, target)
        if Path(origin).name == 'native-ocr':
            Path(target).write_bytes(b'substituted during copy')
    with patch('scripts.native_package.verify_native_model_assets'), patch('scripts.native_package.shutil.copy2', side_effect=corrupted_copy):
        with pytest.raises(ValueError, match='changed during staging'):
            stage_native_artifact(source, tmp_path / 'target', 'macos', provider='coreml')


def test_coreml_workflow_and_diagnostic_payload_boundaries():
    from scripts.package_release import include_source
    from scripts.release_guard import NATIVE_COREML_CHECKS, required_acceptance_checks
    for name in ('runner.cpp', 'benchmark.py', 'CMakeLists.txt', 'README.md'):
        assert not include_source(Path('scripts/native_diagnostics') / name, 'macos', 'arm64')
    workflow = (ROOT / '.github/workflows/native-ocr.yml').read_text()
    assert 'profile: native-coreml' in workflow
    assert "--accelerator ${{ matrix.platform == 'macos' && 'coreml' || 'cpu' }}" in workflow
    assert 'release-candidate-${{ matrix.platform }}-${{ matrix.arch }}-${{ matrix.profile }}' in workflow
    assert 'native-coreml-diagnostic-frontend-${{ github.sha }}' in workflow
    assert set(NATIVE_COREML_CHECKS) <= set(required_acceptance_checks('native-coreml'))
    assert not set(NATIVE_COREML_CHECKS) & set(required_acceptance_checks('native-cpu'))
