"""Source-only CI cannot silently replace native release or claim GPU proof."""
from pathlib import Path
import json
import os
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[1]
PATHS = (
    '.github/workflows/python-source.yml', '.github/actionlint.yaml', '.gitattributes',
    'requirements-coreml-inference.txt', 'backend/tools/python_coreml_ocr.py',
    'backend/tools/python_coreml_runtime.json', 'backend/tools/native_ocr.py',
    'backend/tools/hardware_accelerator.py', 'backend/tools/paddle_model_config.py',
    'backend/tools/ocr.py', 'backend/tools/subtitle_detect.py', 'backend/tools/directml_ocr.py',
    'backend/main.py', 'backend/cli.py', 'ui/setting_interface.py',
    'scripts/prepare_source_models.py', 'scripts/check_python_coreml.py',
    'scripts/e2e_python_coreml.py', 'scripts/e2e_smoke.py', 'scripts/paddle_package_models.py',
    'native/ocr/model-source-pins.json', 'native/ocr/tests/evidence/**',
    'native/ocr/tests/goldens/smoke.json', 'tests/test_python_coreml*.py',
    'tests/test_check_python_coreml.py', 'tests/test_prepare_source_models.py', 'tests/test_e2e_python_coreml.py',
    'tests/test_python_source_workflow.py', 'README.md', 'README_en.md', 'docs/USAGE.md',
)


def workflow():
    return (ROOT / '.github/workflows/python-source.yml').read_text(encoding='utf-8')


def test_source_installs_into_new_venv_and_never_builds_custom_worker():
    value = workflow()
    for required in ('ubuntu-24.04', 'macos-26', "python-version: '3.12'", 'python -m venv',
                     'requirements-coreml-inference.txt', 'python -m pip check',
                     'python -m pip freeze', 'custom_worker_files', 'selected_backend()',
                     'VSE_OCR_BACKEND=python-coreml', 'absent-native-worker'):
        assert required in value
    for forbidden in ('native/ocr/build.py', 'prepare_models.py', 'cmake --build',
                      'gh release', 'contents: write', 'continue-on-error:', 'gh api', 'GITHUB_TOKEN',
                      'actions/download-artifact', 'DYLD_LIBRARY_PATH', 'MTL_CAPTURE_ENABLED'):
        assert forbidden not in value
    assert "ORT_DISABLE_TELEMETRY: '1'" in value
    assert 'defaults:\n  run:\n    shell: bash' in value


def test_model_data_is_public_reverified_and_no_prior_action_artifact_is_required():
    value = workflow()
    assert 'prepare_source_models.py onnx --output build/source-models' in value
    assert 'prepare_source_models.py verify --format onnx build/source-models' in value
    assert 'source-model-data-60e6f8098c058777273b5a6820d804731b5c93c45c78b14f5d8d1d1165361a11' in value
    assert 'restore-keys:' not in value
    assert '--version 6.10.7' in value
    assert '--verify-only' in value


def test_real_source_cases_video_gui_and_failure_artifacts_cannot_be_skipped_by_unit_success():
    value = workflow()
    assert 'python gui.py --smoke-test' in value
    for provider, case in (('coreml', 'early'), ('coreml', 'models'), ('cpu', 'all'), ('coreml', 'all')):
        assert f'--provider {provider} --case-set {case}' in value
    assert 'scripts/e2e_python_coreml.py' in value
    assert 'tests/test_check_python_coreml.py' in value
    assert value.index('--case-set early') < value.index('--case-set models') < value.index('--provider coreml --case-set all')
    artifact = value.split('- name: Preserve source evidence including genuine failures', 1)[1].split('- name:', 1)[0]
    assert 'if: always()' in artifact and 'test-results/python-source/' in artifact
    assert 'not physical GPU' in value


def test_only_a_separate_diagnostic_snapshot_can_add_its_exact_trigger():
    value = workflow()
    assert '  workflow_dispatch:' in value
    release = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
    standard = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    if '  push:' in value:
        trigger = value.split('  push:', 1)[1].split('  workflow_dispatch:', 1)[0]
        assert "branches: ['fix/python-source-coreml']" in trigger
        paths = tuple(line.strip()[2:].strip("'\"") for line in trigger.splitlines() if line.strip().startswith('- '))
        assert paths == PATHS
        expected = "branches: ['**', '!fix/python-source-coreml']"
    else:
        expected = "branches: ['**']"
    for file in (release, standard):
        assert expected in file.split('  push:', 1)[1].split('  pull_request:', 1)[0]
        assert '  pull_request:' in file and '  workflow_dispatch:' in file


def test_host_loader_cleanup_keeps_the_strict_source_guard_and_precedes_installation():
    value = workflow()
    assert value.index('Prove the fresh interpreter starts') < value.index('Install public Python source dependencies')
    assert "name.startswith(('LD_', 'DYLD_'))" in value
    assert "[str(interpreter), '-I', '-c', code]" in value
    assert 'env=clean, check=True' in value
    assert 'clean-interpreter.json' in value
    checker = (ROOT / 'scripts/check_python_coreml.py').read_text(encoding='utf-8')
    assert 'Start a fresh Python source process without loader overrides' in checker
    assert '/opt/hostedtoolcache' not in checker


def test_actual_workflow_cleanup_starts_a_fresh_venv_without_poisoned_loader_values(tmp_path):
    step = workflow().split('- name: Prove the fresh interpreter starts', 1)[1].split('- name:', 1)[0]
    code = textwrap.dedent(step.split("python - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0])
    venv = tmp_path / 'python-source-venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    (tmp_path / 'test-results/python-source').mkdir(parents=True)
    environment_file = tmp_path / 'github-env'
    environment_file.write_text('EXISTING_VALUE=preserved\n')
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_ENV=str(environment_file),
               LD_LIBRARY_PATH=str(tmp_path / 'not-the-python-library'),
               DYLD_LIBRARY_PATH=str(tmp_path / 'not-the-runtime'),
               DYLD_FALLBACK_LIBRARY_PATH=str(tmp_path / 'not-the-fallback'))
    result = subprocess.run([sys.executable, '-I', '-c', code], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / 'test-results/python-source/clean-interpreter.json').read_text())
    assert report['probe']['loader_overrides'] == {}
    assert Path(report['probe']['prefix']).resolve() == venv.resolve()
    written = environment_file.read_text()
    assert written.startswith('EXISTING_VALUE=preserved\n')
    for name in ('LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH'):
        assert name in report['removed_loader_variable_names']
        assert name + '=\n' in written
    assert 'not-the-' not in written
