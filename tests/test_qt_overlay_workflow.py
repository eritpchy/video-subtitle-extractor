"""Qt candidate resources cannot promote admission or bypass source/dist gates."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_resource_builder_is_read_only_explicit_and_has_no_publication_trigger():
    text = (ROOT / '.github/workflows/qt-overlay-resources.yml').read_text()
    assert '  workflow_call:' in text and '  workflow_dispatch:' in text
    assert '  push:' not in text
    for forbidden in ('contents: write', 'secrets: inherit', 'continue-on-error', 'curl ', 'gh release', 'git push'):
        assert forbidden not in text
    assert 'GH_TOKEN: ${{ github.token }}' in text
    assert 'python -I -S scripts/qt_overlay_resources_ci.py --output build/qt-overlay-input' in text
    assert '--input-contract preserved-8f-attempt2' in text
    assert 'path: build/qt-overlay-input/assets/' in text
    assert 'if: always()' in text and 'path: build/qt-overlay-input/reports/' in text


def test_same_run_resource_reuse_is_internal_and_does_not_adopt_failed_producer():
    for name, job in [('python-source.yml', 'source'), ('native-ocr.yml', 'native')]:
        text = (ROOT / '.github/workflows' / name).read_text()
        manual, interface = text.split('  workflow_call:', 1)
        assert 'reuse_qt_overlay_resources' not in manual
        assert 'reuse_qt_overlay_resources:' in interface
        assert 'if: ${{ !inputs.reuse_qt_overlay_resources }}' in text
        block = text.split('  ' + job + ':\n', 1)[1]
        assert "needs.qt-overlay-resources.result == 'success'" in block
        assert "inputs.reuse_qt_overlay_resources && needs.qt-overlay-resources.result == 'skipped'" in block
        assert 'name: qt-overlay-resources-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}' in block
        start = block.index('name: qt-overlay-resources-${{ github.sha }}')
        download = block[start:].split('      - ', 1)[0]
        for forbidden in ('run-id:', 'repository:', 'github-token:', 'pattern:'):
            assert forbidden not in download
    release = (ROOT / '.github/workflows/release.yml').read_text()
    assert release.count('      reuse_qt_overlay_resources: true') == 2
    assert 'needs: [original-models, qt-overlay-resources]' in release
    assert 'needs: qt-overlay-resources' in release
    assert 'python scripts/package_macos_qt_overlay.py --archives dist' in release


def test_mac_preparation_is_explicit_before_gui_and_package_sealing():
    source = (ROOT / '.github/workflows/python-source.yml').read_text()
    assert source.index('--step pip-check') < source.index('--step qt-overlay') < source.index('--step gui-smoke')
    assert 'prepare_macos_qt.py --apply' not in source  # One actual invocation via audited stepper.
    native = (ROOT / '.github/workflows/native-ocr.yml').read_text()
    assert '--qt-overlay-candidate-testing --qt-overlay-archive ' in native
    assert '--qt-overlay-source build/qt-overlay-resources/vse-qtbase6.12.0-ax772484-ps6-source.tar.xz' in native
    assert "matrix.platform == 'macos' && '--qt-overlay" in native
    policy = json.loads((ROOT / 'native/qt/macos-qt-overlay.json').read_text())
    assert policy['production_integration_allowed'] is False
    assert policy['resource']['sha256'] == '9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441'
    assert policy['source_resource']['sha256'] == '5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd'


def test_macos_constraints_are_platform_scoped_and_preserve_non_mac_qt():
    constraints = (ROOT / 'requirements-macos-qt.txt').read_text().splitlines()
    lines = [line for line in constraints if line and not line.startswith('#')]
    assert len(lines) == 4
    assert all('==6.12.0 ; sys_platform == "darwin"' in line for line in lines)
    for name in ('requirements.txt', 'requirements-coreml-inference.txt', 'requirements-native-inference.txt', 'requirements-inference.txt'):
        text = (ROOT / name).read_text()
        assert '~=6.11 ; sys_platform != "darwin"' in text
        assert '==6.12.0 ; sys_platform == "darwin"' in text


def test_same_run_source_identity_includes_actual_mac_controls():
    from scripts.source_acceptance import contract_files
    for path in ('ui/setting_interface.py', 'ui/advanced_setting_interface.py', 'ui/macos_setting_controls.py'):
        assert path in contract_files()

    for workflow in ('native-ocr.yml', 'python-source.yml'):
        assert 'tests/test_macos_setting_controls.py' in (ROOT / '.github/workflows' / workflow).read_text()
        assert 'tests/test_macos_setting_control_paint.py' in (ROOT / '.github/workflows' / workflow).read_text()
