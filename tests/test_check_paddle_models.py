"""Offline contract tests; synthetic outcomes are never inference evidence."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import check_paddle_models as checker

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'native/ocr/tests/evidence'


def synthetic_report(identity=None):
    """Build a clearly synthetic frozen-case receipt for consumer unit tests."""
    cases = checker.load_cases(FIXTURES)
    for row in cases:
        row['actual'] = copy.deepcopy(row['expected'])
        if row['operation'] == 'ocr':
            row.update(checker.compare_ocr(row['actual'], row['expected']))
        else:
            row.update(passed=True, same_boxes=True)
    return {'synthetic_unit_fixture': True, 'schema': 1, 'result': 'passed', 'ocr_backend': 'paddle',
            'cpu_forced': True, 'gpu_inference_verified': False, 'model_count': 15,
            'modes': list(checker.MODES), 'scope': 'final-bundle' if identity else 'source',
            'bundled_python_verified': identity is not None, **(identity or {}),
            'confidence_tolerance': checker.TOLERANCE, 'source_pins_sha256': checker.SOURCE_PINS_SHA256,
            'golden_sha256': checker.GOLDEN_SHA256, 'fixture_manifest_sha256': checker.FIXTURE_MANIFEST_SHA256,
            'case_proof_sha256': checker.CASE_PROOF_SHA256,
            'models_unchanged': True, 'production_modules_unchanged': True, 'network_attempts': [],
            'device': {'paddle_device': 'cpu', 'hardware_enabled': False, 'inference_kwargs': {'device': 'cpu'}},
            'covered_models': sorted(checker.ALL_MODELS), 'cases': cases}


class PaddleModelGateTests(unittest.TestCase):
    def test_fixed_suite_has_all_modes_real_operations_and_kannada_aliases(self):
        cases = checker.load_cases(FIXTURES)
        self.assertEqual(len(cases), 168)
        for mode in checker.MODES:
            rows = [row for row in cases if row['mode'] == mode]
            self.assertEqual(len(rows), 56)
            self.assertEqual(sum(row['operation'] == 'ocr' for row in rows), 45)
            self.assertEqual(sum(row['operation'] == 'detect' for row in rows), 11)
            self.assertEqual({row['language'] for row in rows if row['rec_model'] == 'ka_PP-OCRv3_mobile_rec'}, {'kn', 'ka'})
        self.assertEqual(checker._case_proof(cases), checker.CASE_PROOF_SHA256)

    def test_arabic_uses_original_paddle_display_reference_without_conversion(self):
        rows = checker.load_cases(FIXTURES)
        row = next(row for row in rows if row['mode'] == 'fast' and row['language'] == 'ar' and row['fixture'].endswith('-script.png'))
        self.assertEqual(row['expected'][1][0][0], 'مرحبا بالعالم')
        self.assertEqual(row['baseline'], 'legacy/pipeline-comparison.json')
        automatic = next(row for row in rows if row['mode'] == 'auto' and row['language'] == 'ar' and row['fixture'].endswith('-script.png'))
        self.assertEqual(automatic['expected'], row['expected'])
        self.assertEqual(automatic['baseline_mode'], 'fast')

    def test_source_and_final_identity_contracts(self):
        checker.validate_report(synthetic_report())
        identity = {'commit': 'a' * 40, 'version': '2.3.0', 'build_profile': 'paddle-cpu'}
        checker.validate_report(synthetic_report(identity), identity)
        with self.assertRaisesRegex(ValueError, 'final-bundle'):
            checker.validate_report(synthetic_report(), identity)
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            checker.validate_report(synthetic_report(identity), dict(identity, commit='b' * 40))

    def test_fake_scope_or_interpreter_provenance_cannot_pass(self):
        report = synthetic_report()
        report['scope'] = 'final-bundle'
        with self.assertRaisesRegex(ValueError, 'bundled Python'):
            checker.validate_report(report)

    def test_summary_and_model_hashes_cannot_replace_executed_cases(self):
        report = synthetic_report()
        report['cases'] = []
        report['summary'] = {'passed': 168, 'failed': 0}
        with self.assertRaisesRegex(ValueError, 'Missing or duplicated'):
            checker.validate_report(report)

    def test_duplicate_ids_or_missing_auto_records_are_rejected(self):
        report = synthetic_report()
        report['cases'][-1] = copy.deepcopy(report['cases'][0])
        with self.assertRaises(ValueError):
            checker.validate_report(report)
        report = synthetic_report()
        report['cases'] = [row for row in report['cases'] if row['mode'] != 'auto']
        with self.assertRaises(ValueError):
            checker.validate_report(report)

    def test_case_failures_are_not_overruled_by_success_summary(self):
        report = synthetic_report()
        report['cases'][0]['passed'] = False
        with self.assertRaisesRegex(ValueError, 'Unsuccessful'):
            checker.validate_report(report)

    def test_self_consistent_empty_expected_and_actual_forgery_is_rejected(self):
        report = synthetic_report()
        row = report['cases'][0]
        row['expected'] = row['actual'] = [[], []]
        row.update(checker.compare_ocr(row['actual'], row['expected']))
        self.assertTrue(row['passed'])
        with self.assertRaisesRegex(ValueError, 'canonical frozen proof'):
            checker.validate_report(report)

    def test_fixture_hash_or_actual_model_selection_tampering_is_rejected(self):
        for key, value in [('fixture_sha256', '0' * 64), ('rec_model', 'other_rec'), ('mode', 'accurate')]:
            with self.subTest(key=key):
                report = synthetic_report()
                report['cases'][0][key] = value
                with self.assertRaises(ValueError):
                    checker.validate_report(report)

    def test_actual_text_confidence_and_detection_differences_fail(self):
        report = synthetic_report()
        report['cases'][0]['actual'][1][0][0] = 'wrong text'
        with self.assertRaisesRegex(ValueError, 'OCR outcome'):
            checker.validate_report(report)
        report = synthetic_report()
        report['cases'][0]['actual'][1][0][1] -= .001
        with self.assertRaisesRegex(ValueError, 'OCR outcome'):
            checker.validate_report(report)
        report = synthetic_report()
        row = next(row for row in report['cases'] if row['operation'] == 'detect')
        row['actual'] = []
        with self.assertRaisesRegex(ValueError, 'Detector outcome'):
            checker.validate_report(report)

    def test_network_gpu_and_changed_models_cannot_pass(self):
        for key, value in [('network_attempts', ['blocked attempt']), ('gpu_inference_verified', True),
                           ('cpu_forced', False), ('models_unchanged', False), ('production_modules_unchanged', False)]:
            with self.subTest(key=key):
                report = synthetic_report()
                report[key] = value
                with self.assertRaises(ValueError):
                    checker.validate_report(report)

    def test_fixture_trust_root_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'evidence').mkdir()
            (root / 'goldens').mkdir()
            (root / 'evidence/fixture-manifest.json').write_text('{}')
            (root / 'goldens/smoke.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'trust root'):
                checker.load_cases(root / 'evidence')

    def test_module_outside_app_root_is_rejected(self):
        module = SimpleNamespace(__name__='backend.tools.ocr', __file__=str(ROOT / 'backend/tools/ocr.py'))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'outside app-root'):
                checker._module_origin(module, Path(temporary).resolve())
        self.assertEqual(checker._module_origin(module, ROOT), [Path(module.__file__).resolve()])

    def test_fake_manifest_cannot_promote_host_python_to_final_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'runtime').mkdir()
            executable = root / 'runtime/python'
            executable.write_bytes(b'not a running interpreter')
            with self.assertRaisesRegex(ValueError, 'actual bundled Python'):
                checker._check_bundle_runtime(root, {'runtime_python': 'runtime/python'})
            with patch.object(checker.sys, 'executable', str(executable)):
                checker._check_bundle_runtime(root, {'runtime_python': 'runtime/python'})
            with self.assertRaises(ValueError):
                checker._check_bundle_runtime(root, {'runtime_python': '../outside'})

    def test_environment_is_restored_and_network_guard_never_fetches(self):
        import os
        with patch.dict(os.environ, {'VSE_OCR_BACKEND': 'native'}):
            with checker._environment({'VSE_OCR_BACKEND': 'paddle'}):
                self.assertEqual(os.environ['VSE_OCR_BACKEND'], 'paddle')
            self.assertEqual(os.environ['VSE_OCR_BACKEND'], 'native')
        guard = checker.NetworkGuard()
        with guard:
            with self.assertRaisesRegex(RuntimeError, 'forbids network'):
                guard._audit('socket.connect', ('not actually opened',))
        self.assertEqual(len(guard.attempts), 1)
        guard._audit('socket.connect', ('disabled',))
        self.assertEqual(len(guard.attempts), 1)

    def test_executor_really_calls_auto_separately_for_every_fixture(self):
        cases = checker.load_cases(FIXTURES)
        expected = {(row['mode'], row['language'], row['operation'], str((FIXTURES / row['fixture']).resolve())): row['expected'] for row in cases}
        calls = []
        class Engine:
            def __init__(self, language, mode, *unused):
                self.language, self.mode = language, mode
            def predict(self, image):
                calls.append((self.mode, self.language, 'ocr'))
                return copy.deepcopy(expected[(self.mode, self.language, 'ocr', image)])
            def detect_subtitle(self, image):
                calls.append((self.mode, self.language, 'detect'))
                polygons = copy.deepcopy(expected[(self.mode, self.language, 'detect', image)])
                return SimpleNamespace(tolist=lambda: polygons), .01
            def close(self):
                pass
        def config(hardware, language, mode):
            tier = 'medium' if mode == 'accurate' else 'small'
            name = (f'PP-OCRv6_{tier}_rec' if language == 'ch' else 'ka_PP-OCRv3_mobile_rec' if language == 'ka' else
                    next(name for name, value in checker.LEGACY.items() if value == language))
            return SimpleNamespace(DET_MODEL_NAME=f'PP-OCRv6_{tier}_det', REC_MODEL_NAME=name)
        runtime = SimpleNamespace(cv2=SimpleNamespace(imread=lambda path: path), hardware=object(),
                                  ModelConfig=config, OcrRecogniser=Engine, SubtitleDetect=Engine)
        rows = []
        checker._execute(cases, FIXTURES.resolve(), runtime, rows.append)
        self.assertEqual(len(calls), 168)
        self.assertTrue(all(row['passed'] for row in rows))
        for mode in checker.MODES:
            self.assertEqual(sum(call[0] == mode for call in calls), 56)

    def test_validator_imports_without_site_packages_or_fixture_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'synthetic.json'
            path.write_text(json.dumps(synthetic_report()))
            code = ('import importlib.util,json;'
                    f's=importlib.util.spec_from_file_location("gate",{checker.__file__!r});'
                    'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                    f'm.validate_report(json.load(open({str(path)!r})));print("ok")')
            result = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), 'ok')


if __name__ == '__main__':
    unittest.main()
