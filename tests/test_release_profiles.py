"""Dependency-light tests for truthful package identity and dependency selection."""
from pathlib import Path
import unittest
import subprocess
import sys
import tempfile
from unittest.mock import patch

from scripts.release_profiles import PROFILES, CANDIDATE_TARGETS, LEGACY_PROFILE_GAPS, BLOCKED_PROFILES, archive_name, profile_identity, profile_requirements
from scripts.prepare_release_profile import prepare

ROOT = Path(__file__).resolve().parents[1]


class ReleaseProfileTests(unittest.TestCase):
    def test_direct_script_packager_can_load_trusted_directml_staging(self):
        # -I removes cwd/PYTHONPATH: emulate the supported scripts/ entrypoint
        # rather than accidentally relying on pytest having added repo root.
        code = (f'import sys; sys.path.insert(0, {str(ROOT / "scripts")!r}); '
                'import package_release; import scripts.directml_package; '
                'assert scripts.directml_package.ORT_VERSION == "1.20.1"')
        with tempfile.TemporaryDirectory() as outside:
            subprocess.run([sys.executable, '-I', '-c', code], cwd=outside,
                           check=True, capture_output=True, text=True)

    def test_approved_matrix_is_seven_distinct_packages(self):
        self.assertEqual(len(CANDIDATE_TARGETS), 7)
        names = {archive_name('2.3.0', *key) for key in CANDIDATE_TARGETS}
        self.assertEqual(len(names), 7)
        self.assertIn('vse-2.3.0-windows-x64-directml.zip', names)
        self.assertIn('vse-2.3.0-windows-x64-cuda11.8.zip', names)
        self.assertIn('vse-2.3.0-linux-x64-native-cpu.zip', names)
        self.assertFalse(any('python312' in name or 'cuda10.2' in name for name in names))

    def test_historical_cuda10_is_not_faked_or_silently_reintroduced(self):
        self.assertIn('paddle-cuda10.2', LEGACY_PROFILE_GAPS)
        self.assertEqual(BLOCKED_PROFILES, {})  # User-approved migration; not publication authorization.
        with self.assertRaises(ValueError):
            profile_identity('paddle', 'cuda10.2')

    def test_cpu_and_native_names_remain_compatible(self):
        self.assertEqual(profile_identity('paddle')[0], 'paddle-cpu')
        self.assertEqual(profile_identity('native')[0], 'native-cpu')
        self.assertEqual(profile_identity('directml')[0], 'directml')
        with self.assertRaises(ValueError):
            profile_identity('native', 'cuda11.8')

    def test_cuda_replaces_not_adds_paddle_root(self):
        for profile in ('paddle-cuda11.8', 'paddle-cuda12.6'):
            roots = profile_requirements(ROOT, profile)
            self.assertIn('paddlepaddle-gpu==3.3.1', roots)
            self.assertFalse(any(root.startswith('paddlepaddle==') for root in roots))
            self.assertIn('paddleocr==3.7.0', roots)
            self.assertIn('paddlex[ocr-core]==3.7.2', roots)
            self.assertIn('pyside6-essentials~=6.11', roots)
        self.assertIn('paddlepaddle==3.3.1', profile_requirements(ROOT, 'paddle-cpu'))
        self.assertIn('nvidia-nvjitlink-cu12==12.6.85', profile_requirements(ROOT, 'paddle-cuda12.6'))
        self.assertNotIn('nvidia-nvjitlink-cu12==12.6.85', profile_requirements(ROOT, 'paddle-cuda11.8'))

    def test_original_directml_runtime_only(self):
        roots = profile_requirements(ROOT, 'directml')
        self.assertIn('onnxruntime-directml==1.20.1', roots)
        self.assertFalse(any(root.startswith(('paddle', 'onnxruntime==', 'onnxruntime-gpu')) for root in roots))

    def test_gpu_names_are_windows_x64_only(self):
        for profile in ('directml', 'paddle-cuda11.8', 'paddle-cuda12.6'):
            for target in (('linux', 'x64'), ('macos', 'arm64'), ('windows', 'arm64')):
                with self.assertRaises(ValueError):
                    archive_name('2.3.0', *target, profile)

    def test_workflow_is_automatic_but_never_publishes(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('  push:', workflow)
        self.assertIn("branches: ['**']", workflow)
        self.assertNotIn('contents: write', workflow)
        self.assertNotIn('--publish', workflow)
        self.assertNotIn('softprops/action-gh-release', workflow)
        self.assertIn('windows-2022', workflow)
        self.assertIn('windows-2025', workflow)
        self.assertIn('accept_release_archive.py', workflow)
        for profile in ('paddle-cpu', 'paddle-cuda11.8', 'paddle-cuda12.6', 'directml'):
            self.assertIn(profile, workflow)
        native = (ROOT / '.github/workflows/native-ocr.yml').read_text()
        self.assertIn('  workflow_call:', native)
        self.assertNotIn('  push:', native)  # No duplicate Native matrix per branch push.


if __name__ == '__main__':
    unittest.main()
