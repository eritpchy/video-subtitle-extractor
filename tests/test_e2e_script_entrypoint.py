"""Real isolated Python entrypoints; no Mac, video, OCR or provider is mocked."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/e2e_smoke.py'
VALIDATOR = ROOT / 'backend/tools/native_ocr.py'


class EvidenceScriptEntrypointTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='vse-evidence-entrypoint-')
        self.addCleanup(directory.cleanup)
        self.cwd = Path(directory.name).resolve()
        self.evidence = self.cwd / 'evidence'
        self.evidence.mkdir()
        self.marker = self.cwd / 'foreign-backend-executed'
        backend = self.cwd / 'backend'
        backend.mkdir()
        (backend / '__init__.py').write_text(
            'from pathlib import Path\n'
            f'Path({str(self.marker)!r}).write_text("UNTRUSTED")\n'
            'raise RuntimeError("polluted working directory backend executed")\n')
        self.env = dict(os.environ, PYTHONPATH=str(self.cwd))

    def collect(self, flags, expected, *, preload=False):
        code = f'''
import hashlib, importlib.util, json, runpy, sys, types
from pathlib import Path
if {preload!r}:
    foreign = types.ModuleType('backend')
    foreign.__file__ = {str(self.cwd / 'backend/__init__.py')!r}
    foreign.__spec__ = importlib.util.spec_from_file_location('backend', foreign.__file__)
    sys.modules['backend'] = foreign
script = runpy.run_path({str(SCRIPT)!r}, run_name='evidence_entrypoint_regression')
report = {{'mode': 'fast', 'requested_provider': 'coreml', 'cpu_forced': False,
           'gpu_inference_verified': False}}
try:
    script['collect_coreml_video_evidence'](Path({str(self.evidence)!r}), report)
except (ValueError, RuntimeError) as error:
    assert {expected!r} in str(error), (type(error).__name__, str(error))
else:
    raise AssertionError('Invalid evidence was accepted')
assert not any(name in sys.modules for name in ('cv2', 'numpy', 'PySide6', 'paddle', 'onnxruntime'))
module = sys.modules.get('backend.tools.native_ocr')
print(json.dumps({{'validator': str(Path(module.__file__).resolve()) if module else None,
                  'sha256': hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() if module else None,
                  'foreign_executed': Path({str(self.marker)!r}).exists()}}))
'''
        result = subprocess.run([sys.executable, *flags, '-c', code], cwd=self.cwd,
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['foreign_executed'])
        return report

    def test_isolated_and_polluted_entrypoints_use_exact_script_root(self):
        for flags in (('-I', '-S'), ('-S',)):
            with self.subTest(flags=flags):
                report = self.collect(flags, 'Missing explicit CoreML video execution evidence')
                self.assertEqual(report['validator'], str(VALIDATOR))
                self.assertEqual(report['sha256'], hashlib.sha256(VALIDATOR.read_bytes()).hexdigest())

    def test_preloaded_foreign_backend_is_rejected_not_silently_reused(self):
        report = self.collect(('-I', '-S'), 'outside the trusted source root', preload=True)
        self.assertIsNone(report['validator'])

    def test_duplicate_json_guard_is_still_the_real_shared_validator(self):
        (self.evidence / 'native-ocr-1-1.json').write_text('{"worker_pid":1,"worker_pid":2}')
        self.collect(('-I', '-S'), 'Duplicate native OCR JSON field')

    def test_sixteen_mib_limit_is_unchanged_in_isolated_script(self):
        with (self.evidence / 'native-ocr-1-1.json').open('wb') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        self.collect(('-I', '-S'), 'Unexpected CoreML video request evidence')

    def test_direct_script_help_does_not_need_repo_cwd_or_frameworks(self):
        result = subprocess.run([sys.executable, '-I', '-S', str(SCRIPT), '--help'],
                                cwd=self.cwd, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('--provider', result.stdout)
        self.assertFalse(self.marker.exists())


if __name__ == '__main__':
    unittest.main()
