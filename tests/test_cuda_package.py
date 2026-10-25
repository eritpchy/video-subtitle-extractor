"""No real Windows/GPU dependency: test the final vendor-byte safety contract."""
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.cuda_package import audit


class Entry:
    def __init__(self, relative, data):
        self.relative = relative
        self.size = len(data)
        self.hash = SimpleNamespace(mode='sha256', value=base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='))
    def __str__(self):
        return self.relative


class Distribution:
    def __init__(self, packages, name, version, relative):
        self.metadata = {'Name': name}
        self.version = version
        self.packages = packages
        data = b'unchanged official runtime fixture'
        path = packages / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files = [Entry(relative, data)]
    def locate_file(self, entry):
        return self.packages / str(entry)


class CudaPackageTests(unittest.TestCase):
    def test_cuda_search_overrides_are_removed_without_changing_device_selection(self):
        from scripts.bundle_bootstrap import NATIVE_SEARCH_VARIABLES, sanitize_environment
        incoming = {name: '/foreign/cuda' for name in NATIVE_SEARCH_VARIABLES
                    if name.startswith('FLAGS_') or name.startswith(('CUDA_', 'CUDNN_'))}
        incoming.update(CUDA_VISIBLE_DEVICES='0', FLAGS_fraction_of_gpu_memory_to_use='0.5')
        clean = sanitize_environment(Path('/bundle'), incoming, 'linux')
        for name in incoming:
            if name not in ('CUDA_VISIBLE_DEVICES', 'FLAGS_fraction_of_gpu_memory_to_use'):
                self.assertNotIn(name, clean)
        self.assertEqual(clean['CUDA_VISIBLE_DEVICES'], '0')
        self.assertEqual(clean['FLAGS_fraction_of_gpu_memory_to_use'], '0.5')

    def fixture(self, root):
        packages = root / 'runtime/Lib/site-packages'
        records = [Distribution(packages, 'paddlepaddle-gpu', '3.3.1', 'paddle/base/libpaddle.pyd'),
                   Distribution(packages, 'nvidia-cublas-cu11', '11.11.3.6', 'nvidia/cublas/bin/cublas64_11.dll')]
        paddle = SimpleNamespace(__file__=str(packages / 'paddle/__init__.py'), __version__='3.3.1',
                                 is_compiled_with_cuda=lambda: True,
                                 version=SimpleNamespace(cuda=lambda: '11.8', cudnn=lambda: '8.9.6', compiled_cuda_archs=[61, 70, 75, 80, 86]))
        return packages, records, paddle

    def test_compiled_identity_and_bytes_are_not_hardware_certification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages, records, paddle = self.fixture(root)
            with patch('scripts.cuda_package.sys.platform', 'win32'), patch('scripts.cuda_package.distributions', return_value=records), patch.dict('sys.modules', {'paddle': paddle, 'PySide6': SimpleNamespace()}):
                report = audit(root, 'cuda11.8', prepare=True)
                self.assertFalse(report['gpu_inference_verified'])
                self.assertFalse(report['gpu_device_probe_performed'])
                self.assertEqual(report['compiled_cuda_archs'][0], 61)
                self.assertEqual(len(report['runtime_files']), 2)
                self.assertEqual(audit(root, 'cuda11.8')['runtime_files'], report['runtime_files'])
                (packages / records[1].files[0].relative).write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, 'Changed vendor'):
                    audit(root, 'cuda11.8')

    def test_cpu_gpu_mixture_and_wrong_cuda_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages, records, paddle = self.fixture(root)
            with patch('scripts.cuda_package.sys.platform', 'win32'), patch('scripts.cuda_package.distributions', return_value=records), patch.dict('sys.modules', {'paddle': paddle, 'PySide6': SimpleNamespace()}):
                with self.assertRaisesRegex(RuntimeError, 'does not match'):
                    audit(root, 'cuda12.6', prepare=True)
                records.append(Distribution(packages, 'paddlepaddle', '3.3.1', 'cpu.dll'))
                with self.assertRaisesRegex(RuntimeError, 'CPU Paddle'):
                    audit(root, 'cuda11.8', prepare=True)

    def test_missing_vendor_runtime_cannot_borrow_host_cuda(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packages, records, paddle = self.fixture(root)
            (packages / records[1].files[0].relative).unlink()
            with patch('scripts.cuda_package.sys.platform', 'win32'), patch('scripts.cuda_package.distributions', return_value=records):
                with self.assertRaisesRegex(RuntimeError, 'Missing/unverifiable'):
                    audit(root, 'cuda11.8', prepare=True)

    def test_foreign_os_is_not_windows_validation(self):
        with tempfile.TemporaryDirectory() as temporary, patch('scripts.cuda_package.sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'native Windows'):
                audit(Path(temporary), 'cuda11.8', prepare=True)


if __name__ == '__main__':
    unittest.main()
