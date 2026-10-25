"""Dependency-light checks: reporting must never turn untested Windows into a pass."""
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts import windows_verification as windows

ROOT = Path(__file__).resolve().parents[1]


def identity(runner='windows-2022'):
    expected = windows.RUNNERS[runner]
    return {'validated': True, 'runner_label': runner, 'profile': 'paddle',
            'os': {**expected, 'update_build_revision': 123},
            'architecture': windows.architecture(0, 0x8664), 'python': {'pointer_bits': 64}}


class IdentityTests(unittest.TestCase):
    def test_native_unknown_machine_means_native_not_x86(self):
        self.assertEqual(windows.architecture(0, 0x8664)['process'], 'x64')
        self.assertFalse(windows.architecture(0, 0x8664)['emulated'])

    def test_x64_on_arm_is_explicitly_emulation(self):
        report = windows.architecture(0x8664, 0xAA64)
        self.assertEqual((report['host'], report['process'], report['emulated']), ('arm64', 'x64', True))
        data = identity()
        data['architecture'] = report
        with self.assertRaisesRegex(RuntimeError, 'emulation'):
            windows.validate_identity(data, 'windows-2022')

    def test_two_server_os_versions_are_checked_not_inferred_from_labels(self):
        for runner in windows.RUNNERS:
            windows.validate_identity(identity(runner), runner)
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
            windows.validate_identity(identity('windows-2025'), 'windows-2022')

    def test_windows_11_shared_kernel_build_is_not_server_2025(self):
        data = identity('windows-2025')
        data['os'].update(caption='Microsoft Windows 11 Enterprise', product_type=1)
        with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
            windows.validate_identity(data, 'windows-2025')

    def test_unknown_isa_and_32_bit_interpreter_fail_closed(self):
        for change in ('unknown', '32-bit'):
            data = identity()
            if change == 'unknown':
                data['architecture'] = windows.architecture(0, 0xFFFF)
            else:
                data['python']['pointer_bits'] = 32
            with self.assertRaises(RuntimeError):
                windows.validate_identity(data, 'windows-2022')

    def test_linux_cannot_emit_fake_windows_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(windows.sys, 'platform', 'linux'):
            output = Path(tmp) / 'identity.json'
            with self.assertRaisesRegex(RuntimeError, 'not simulated'):
                windows.capture(output, 'windows-2022', 'paddle')
            self.assertFalse(json.loads(output.read_text())['validated'])

    def test_capture_preserves_failed_architecture_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(windows.sys, 'platform', 'win32'), \
                patch.object(windows, 'windows_os', return_value=identity()['os']), \
                patch.object(windows, 'windows_architecture', return_value=windows.architecture(0x8664, 0xAA64)):
            output = Path(tmp) / 'identity.json'
            with self.assertRaises(RuntimeError):
                windows.capture(output, 'windows-2022', 'paddle')
            data = json.loads(output.read_text())
            self.assertFalse(data['validated'])
            self.assertTrue(data['architecture']['emulated'])


class CoverageTests(unittest.TestCase):
    def test_every_required_stage_must_pass(self):
        outcomes = dict.fromkeys(windows.COMMON_STAGES, 'success')
        self.assertEqual(windows.coverage(identity(), outcomes, 'paddle')['execution_status'], 'passed')
        for outcome in ('failure', 'cancelled', 'skipped', '', 'invalid'):
            with self.subTest(outcome=outcome):
                current = dict(outcomes, issues=outcome)
                report = windows.coverage(identity(), current, 'paddle')
                self.assertNotEqual(report['execution_status'], 'passed')
                self.assertNotEqual(next(r for r in report['stages'] if r['stage'] == 'issues')['status'], 'passed')

    def test_uncaptured_identity_cannot_pass_even_if_step_says_success(self):
        data = identity()
        data['validated'] = False
        report = windows.coverage(data, dict.fromkeys(windows.COMMON_STAGES, 'success'), 'paddle')
        self.assertEqual(report['stages'][0]['status'], 'failed')

    def test_missing_native_worker_steps_are_not_a_native_pass(self):
        report = windows.coverage(identity(), dict.fromkeys(windows.COMMON_STAGES, 'success'), 'native')
        self.assertEqual(report['stages'][0]['status'], 'not-run')
        self.assertNotEqual(report['execution_status'], 'passed')

    def test_native_success_still_requires_worker_and_no_failed_license_gate(self):
        outcomes = dict.fromkeys(windows.COMMON_STAGES, 'success')
        outcomes.update({'native-build': 'success', 'native-smoke': 'success', 'license-gate': 'skipped'})
        self.assertEqual(windows.coverage(identity(), outcomes, 'native')['execution_status'], 'passed')
        outcomes['license-gate'] = 'failure'
        self.assertNotEqual(windows.coverage(identity(), outcomes, 'native')['execution_status'], 'passed')

    def test_cpu_success_never_claims_client_windows_or_gpu_coverage(self):
        report = windows.coverage(identity(), dict.fromkeys(windows.COMMON_STAGES, 'success'), 'paddle')
        self.assertEqual(report['gpu_execution'], 'outside-ci-only-scope')
        self.assertEqual(report['client_windows_execution'], 'outside-ci-only-scope')
        self.assertEqual(report['windows_arm_execution'], 'outside-ci-only-scope')
        self.assertEqual(report['coverage_policy'], 'server-ci-only-no-client-machine-access')
        summary = windows.markdown(report)
        self.assertIn('not a client Windows certification', summary)
        self.assertIn('client/ARM/GPU machines are not requested or awaited', summary)
        self.assertIn('changing real Windows Settings is outside verified CI coverage', summary)
        self.assertIn('not a clean Windows client VM test', summary)

    def test_license_failure_leaves_customer_package_not_run(self):
        outcomes = dict.fromkeys(windows.COMMON_STAGES, 'success')
        outcomes.update({'native-build': 'success', 'native-smoke': 'success',
                         'license-gate': 'failure', 'package-build': 'skipped', 'package-verify': 'skipped'})
        report = windows.coverage(identity(), outcomes, 'native')
        self.assertEqual(report['customer_package_license_gate'], 'failure')
        self.assertNotEqual(report['execution_status'], 'passed')

    def test_summary_with_missing_capture_is_explicitly_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'VSE_WINDOWS_STEP_OUTCOMES': '{}'}), \
                patch.object(windows, 'dependency_versions', return_value={}):
            with patch.dict(os.environ):
                os.environ.pop('GITHUB_STEP_SUMMARY', None)
                self.assertEqual(windows.main(['summarize', '--runner', 'windows-2025',
                                               '--profile', 'native', '--output', tmp]), 0)
            report = json.loads((Path(tmp) / 'coverage.json').read_text())
            self.assertEqual(report['execution_status'], 'incomplete-or-failed')
            self.assertTrue(all(row['status'] == 'not-run' for row in report['stages']))


class WorkflowContractTests(unittest.TestCase):
    def test_both_cpu_profiles_run_both_real_server_versions_in_parallel(self):
        for name in ('ci.yml', 'native-ocr.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('fail-fast: false\n      max-parallel: 4', text)
            for year in (2022, 2025):
                self.assertIn(f'os: windows-{year}\n            name: Windows Server {year} x64', text)
            self.assertNotIn('os: windows-latest', text)
            self.assertNotIn('os: windows-11-arm', text)
            self.assertNotIn('self-hosted', text)
            self.assertIn('if: always() && runner.os == \'Windows\'', text)
            self.assertIn('scripts/windows_verification.py capture', text)
            self.assertIn('scripts/windows_verification.py summarize', text)

    def test_every_reported_outcome_references_a_real_unique_step(self):
        for name in ('ci.yml', 'native-ocr.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            ids = re.findall(r'^        id: (.+)$', text, re.M)
            self.assertEqual(len(ids), len(set(ids)))
            for step in re.findall(r'\$\{\{ steps\.([\w-]+)\.outcome \}\}', text):
                self.assertIn(step, ids)

    def test_artifact_names_include_os_not_just_windows_x64(self):
        text = (ROOT / '.github/workflows/native-ocr.yml').read_text()
        for artifact in ('evidence', 'candidate'):
            self.assertIn(f'name: native-profile-{artifact}-${{{{ matrix.os }}}}-${{{{ matrix.arch }}}}', text)
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('name: paddle-test-results-${{ matrix.os }}-${{ runner.arch }}', text)

    def test_real_launcher_environment_acceptance_is_a_required_reported_stage(self):
        self.assertIn('portable-environment', windows.COMMON_STAGES)
        for name, distribution in (('ci.yml', 'dist-ci'), ('native-ocr.yml', 'dist-native')):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn(f'python scripts/verify_portable_environment.py --archives {distribution} '
                          '--output test-results/windows/portable-environment', text)
            self.assertIn('VSE_WINDOWS_RUNNER_LABEL: ${{ matrix.os }}', text)
            self.assertIn('"portable-environment":"${{ steps.portable-environment.outcome }}"', text)

    def test_all_native_platforms_have_mandatory_final_zip_environment_gates(self):
        for name, distribution, timeout in (('ci.yml', 'dist-ci', 75), ('native-ocr.yml', 'dist-native', 90)):
            text = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn(f'    timeout-minutes: {timeout}', text)
            for stage, runner, directory in (('portable-environment', 'Windows', 'windows'),
                    ('portable-environment-linux', 'Linux', 'linux'),
                    ('portable-environment-macos', 'macOS', 'macos')):
                section = text.split(f'        id: {stage}\n', 1)[1].split('      - ', 1)[0]
                self.assertIn(f"if: success() && runner.os == '{runner}'", section)
                self.assertIn('timeout-minutes: 35', section)
                self.assertIn(f'--archives {distribution}', section)
                self.assertIn(f'--output test-results/{directory}/portable-environment', section)
                self.assertNotIn('continue-on-error:', section)
                self.assertLess(text.index('        id: package-build'), text.index(f'        id: {stage}\n'))
                if runner == 'Linux':
                    self.assertIn('xvfb-run -a', section)
                    self.assertIn('env QT_QPA_PLATFORM=xcb', section)
                if runner == 'macOS':
                    self.assertIn('QT_QPA_PLATFORM: cocoa', section)
        native = (ROOT / '.github/workflows/native-ocr.yml').read_text()
        self.assertIn('tests/test_portable_environment.py tests/test_portable_python.py', native)
        self.assertIn('    needs: models\n', native)

    def test_existing_models_and_macos_failure_diagnostics_remain_strict(self):
        text = (ROOT / '.github/workflows/native-ocr.yml').read_text()
        self.assertIn('python -S native/ocr/verify_model_cache.py build/native-models', text)
        self.assertIn('Convert all 15 verified official models', text)
        self.assertIn("if: failure() && matrix.platform == 'macos' && steps.native-smoke.outcome == 'failure'", text)
        self.assertIn('native/ocr/precision_diagnostic.py capture', text)
        self.assertIn('native/ocr/precision_diagnostic.py reference', text)
        self.assertNotIn('continue-on-error:', text)

    def test_license_gate_stays_before_package_and_after_source_video_checks(self):
        text = (ROOT / '.github/workflows/native-ocr.yml').read_text()
        self.assertLess(text.index('Windows native multiline, AV1'), text.index('Windows customer-package license gate'))
        self.assertLess(text.index('Windows customer-package license gate'), text.index('Build self-contained native CPU package'))
        self.assertIn("throw 'PENDING: customer packaging requires VC redistribution license acknowledgment", text)


if __name__ == '__main__':
    unittest.main()
