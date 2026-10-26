"""The quick Mac diagnostic cannot replace or weaken the production matrix."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / '.github/workflows/macos-coreml.yml'
DIAGNOSTIC_PATHS = (
    '.github/workflows/macos-coreml.yml', '.github/actionlint.yaml', '.gitattributes',
    'native/ocr/**', 'requirements-native-inference.txt',
    'backend/tools/native_ocr.py', 'backend/tools/hardware_accelerator.py',
    'backend/tools/paddle_model_config.py', 'backend/tools/ocr.py',
    'backend/tools/subtitle_detect.py', 'backend/tools/directml_ocr.py',
    'scripts/native_diagnostics/**', 'tests/test_native_diagnostics.py',
    'scripts/check_native_models.py', 'scripts/check_paddle_models.py', 'scripts/native_package.py',
)


def test_quick_mac_has_exact_source_and_no_conversion_or_publication():
    value = WORKFLOW.read_text(encoding='utf-8')
    assert 'runs-on: macos-26' in value
    assert 'matrix:' not in value
    for field in ('40270545872', '840634db3381f805ee91073c6c0c9192660da356',
                  '121402484190', '12628531578', '263730435',
                  '4cce5e9e5e0ab6956164cd1e24b326c0ab8e4511c08808890e35f78cf82c2897'):
        assert field in value
    for forbidden in ('prepare_models.py', 'requirements-model-build.txt', 'release_guard.py',
                      'gh release', 'contents: write', 'restore-keys:', 'continue-on-error:'):
        assert forbidden not in value
    assert 'ci_model_artifact.py' in value and '--source-job-id' in value
    assert 'ci_coreml_compat.py' in value
    assert '--verify-report test-results/coreml-compatibility/report.json' in value
    assert 'defaults:\n  run:\n    shell: bash' in value
    assert 'python native/ocr/build.py --platform macos --arch arm64' in value


def test_failure_preserves_the_actual_production_worker_and_runtime():
    value = WORKFLOW.read_text(encoding='utf-8')
    step = value.split('- name: Preserve actual production binary/runtime and original failure evidence', 1)[1]
    assert 'if: always()' in step.split('- name:', 1)[0]
    for path in ('build/native-ocr/native-ocr', 'build/native-ocr/*.dylib',
                 'build/native-ocr/*.json', 'build/native-build/worker-build/native-ocr',
                 'native/ocr/ort-sdk-manifest.json', 'test-results/'):
        assert path in step
    assert 'vse-provider-diagnostic' not in step
    assert 'Full publication remains blocked' in value


def test_only_the_isolated_snapshot_may_add_its_precise_push_trigger():
    value = WORKFLOW.read_text(encoding='utf-8')
    assert '  workflow_dispatch:' in value
    release = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
    standard = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    if '  push:' in value:
        trigger = value.split('  push:', 1)[1].split('  workflow_dispatch:', 1)[0]
        assert "branches: ['fix/macos-coreml']" in trigger
        paths = tuple(line.strip()[2:].strip("'\"") for line in trigger.splitlines() if line.strip().startswith('- '))
        assert paths == DIAGNOSTIC_PATHS
        for workflow in (release, standard):
            push = workflow.split('  push:', 1)[1].split('  pull_request:', 1)[0]
            assert "branches: ['**', '!fix/macos-coreml']" in push
    else:
        # This is the main implementation, not its isolated trigger snapshot.
        for workflow in (release, standard):
            push = workflow.split('  push:', 1)[1].split('  pull_request:', 1)[0]
            assert "branches: ['**']" in push
            assert '!fix/macos-coreml' not in push
    assert '  pull_request:' in release and '  pull_request:' in standard
    assert '  workflow_dispatch:' in release and '  workflow_dispatch:' in standard


def test_separate_control_frontend_always_preserves_loader_local_runtime():
    for name in ('macos-coreml.yml', 'native-ocr.yml'):
        value = (ROOT / '.github/workflows' / name).read_text(encoding='utf-8')
        build = value.split('python scripts/native_diagnostics/benchmark.py build', 1)
        assert len(build) == 2
        condition = build[0].rsplit('- name:', 1)[1]
        assert "steps.native-build.outcome == 'success'" in condition
        assert '!cancelled()' in condition
        for argument in ('--sdk build/native-build/ort-sdk', '--deps build/native-build/sources',
                         '--opencv-dir build/native-build/opencv-install/lib/cmake/opencv4',
                         '--output build/native-diagnostic'):
            assert argument in build[1].split('- name:', 1)[0]
        artifact = build[1].split('uses: actions/upload-artifact@v4', 1)
        assert len(artifact) == 2 and 'always()' in artifact[0]
        paths = artifact[1].split('- name:', 1)[0]
        for path in ('build/native-diagnostic/vse-provider-diagnostic',
                     'build/native-diagnostic/libonnxruntime*',
                     'build/native-diagnostic/build-inputs.json',
                     'build/native-diagnostic/build-*.log'):
            assert path in paths
        assert 'DYLD_' not in value
    # Controls remain a different artifact and cannot substitute for the real worker.
    fast = WORKFLOW.read_text(encoding='utf-8')
    assert fast.index('--verify-report') < fast.index('benchmark.py build')
    assert 'production-coreml-worker-${{ github.sha }}' in fast
    assert 'native-coreml-diagnostic-frontend-${{ github.sha }}' in fast
