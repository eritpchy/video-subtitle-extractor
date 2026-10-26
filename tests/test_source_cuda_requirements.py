"""Source dependency/selection contracts, not an installation or CUDA test."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from backend.tools.hardware_accelerator import HardwareAccelerator
from backend.tools.paddle_model_config import PaddleModelConfig
from scripts.release_profiles import PROFILES, profile_requirements

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PROFILES = {'11.8': 'requirements-source-cuda118.txt',
                   '12.6': 'requirements-source-cuda126.txt'}
PIN_PATH = ROOT / 'docs/audits/source-cuda-wheel-pins.json'
# Independent regression anchors from the accepted 59749ccc complete-wheel
# records, not the Range inventory's member CRCs or a claimed vendor signature.
OBSERVED_PADDLE = {
    '11.8': (766607334, '11f5a72624912f4750d4aedf511367672dea698002fbdded316258d45b897146'),
    '12.6': (579382604, 'ad733bfe59cf5978de2b338b6f29a7b3855bfb34d92b65fcb2d88c77aa312cbf'),
}


def lines(name):
    return [line.strip() for line in (ROOT / name).read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def direct_requirements(name):
    entries = lines(name)
    assert entries.count('-r requirements-source-common.txt') == 1
    return [Requirement(line) for line in entries if not line.startswith('-r ')]


class SourceCommonRequirementsTests(unittest.TestCase):
    def test_common_is_exact_source_root_set_without_cpu_paddle(self):
        cpu = lines('requirements.txt')
        self.assertEqual(cpu.count('paddlepaddle==3.3.1'), 1)
        self.assertEqual(lines('requirements-source-common.txt'),
                         [line for line in cpu if line != 'paddlepaddle==3.3.1'])

    def test_source_keeps_full_qt_and_no_dist_substitution_or_converter(self):
        roots = [Requirement(line) for line in lines('requirements-source-common.txt')]
        names = {canonicalize_name(row.name) for row in roots}
        self.assertIn('pyside6', names)
        self.assertNotIn('pyside6-essentials', names)
        self.assertFalse(names & {'paddlepaddle', 'paddlepaddle-gpu', 'paddle2onnx', 'cmake', 'ninja'})
        self.assertIn('paddleocr', names)
        self.assertIn('paddlex', names)


class SourceCudaRequirementsTests(unittest.TestCase):
    def test_reviewed_pins_preserve_the_actual_producer_and_whole_wheel_identity(self):
        pins = json.loads(PIN_PATH.read_text(encoding='utf-8'))
        self.assertEqual(pins['schema'], 1)
        self.assertEqual(set(pins['profiles']), {'cuda11.8', 'cuda12.6'})
        for version, (size, digest) in OBSERVED_PADDLE.items():
            source = pins['profiles']['cuda' + version]
            self.assertEqual(source['source_commit'], '59749ccc02e369f374c69846b2bde6279084577d')
            self.assertEqual(source['source_run'], 40259119763)
            self.assertEqual(source['hash_origin'], 'build-observed complete wheel bytes')
            for key in ('source_archive_sha256', 'source_manifest_sha256'):
                self.assertRegex(source[key], r'^[0-9a-f]{64}$')
            self.assertEqual(source['paddle']['bytes'], size)
            self.assertEqual(source['paddle']['sha256'], digest)
            self.assertEqual(source['paddle']['filename'],
                             'paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl')

    def test_profiles_use_exact_official_variant_wheel_and_complete_hash(self):
        pins = json.loads(PIN_PATH.read_text(encoding='utf-8'))['profiles']
        for version, filename in SOURCE_PROFILES.items():
            with self.subTest(cuda=version):
                roots = direct_requirements(filename)
                paddle = [r for r in roots if canonicalize_name(r.name) == 'paddlepaddle-gpu']
                self.assertEqual(len(paddle), 1)
                url = urlsplit(paddle[0].url)
                cuda_path = urlsplit(PROFILES['paddle-cuda' + version]['gpu_index']).path.rstrip('/')
                self.assertEqual(url.scheme, 'https')
                self.assertEqual(url.netloc, 'paddle-whl.cdn.bcebos.com')
                self.assertEqual(url.path, cuda_path.replace('/packages/', '/') +
                                 '/paddlepaddle-gpu/paddlepaddle_gpu-3.3.1-cp312-cp312-win_amd64.whl')
                self.assertRegex(url.fragment, r'^sha256=[0-9a-f]{64}$')
                self.assertFalse(url.query)
                reviewed = pins['cuda' + version]['paddle']
                self.assertEqual(paddle[0].url, reviewed['url'] + '#sha256=' + reviewed['sha256'])
                self.assertNotIn('paddlepaddle', {canonicalize_name(r.name) for r in roots})

    def test_no_other_platform_python_or_arch_silently_selects_cuda_wheels(self):
        allowed = {'python_version': '3.12', 'implementation_name': 'cpython',
                   'sys_platform': 'win32', 'platform_machine': 'AMD64'}
        rejected = [dict(allowed, implementation_name='pypy'), dict(allowed, sys_platform='linux'), dict(allowed, sys_platform='darwin'),
                    dict(allowed, python_version='3.11'), dict(allowed, python_version='3.13'),
                    dict(allowed, platform_machine='ARM64'), dict(allowed, platform_machine='x86')]
        for filename in SOURCE_PROFILES.values():
            for requirement in direct_requirements(filename):
                self.assertIsNotNone(requirement.marker)
                self.assertTrue(requirement.marker.evaluate(allowed))
                self.assertTrue(requirement.marker.evaluate(dict(allowed, platform_machine='x86_64')))
                for environment in rejected:
                    self.assertFalse(requirement.marker.evaluate(environment))

    def test_cu126_nvjitlink_is_the_already_reviewed_windows_wheel(self):
        inventory = json.loads((ROOT / 'docs/audits/windows-nvjitlink-development-2026-10-25.json').read_text(encoding='utf-8'))
        reviewed = next(r for r in inventory['wheel_inventory']
                        if r['name'] == 'nvidia-nvjitlink-cu12' and r['version'] == '12.6.85')
        source_pin = json.loads(PIN_PATH.read_text(encoding='utf-8'))['profiles']['cuda12.6']['nvjitlink']
        for key in ('url', 'sha256'):
            self.assertEqual(source_pin[key], reviewed[key])
        self.assertEqual(source_pin['bytes'], reviewed['wheel_bytes'])
        for cuda, filename in SOURCE_PROFILES.items():
            names = {canonicalize_name(r.name): r for r in direct_requirements(filename)}
            self.assertEqual(set(names), {'paddlepaddle-gpu'} |
                             ({'nvidia-nvjitlink-cu12'} if cuda == '12.6' else set()))
            if cuda == '12.6':
                self.assertEqual(names['nvidia-nvjitlink-cu12'].url,
                                 reviewed['url'] + '#sha256=' + reviewed['sha256'])
                self.assertIn('nvidia-nvjitlink-cu12==12.6.85', profile_requirements(ROOT, 'paddle-cuda12.6'))


class SourceModeDocumentationTests(unittest.TestCase):
    def test_auto_medium_is_cuda_only_not_a_generic_accelerator_switch(self):
        for backend, cuda in [('paddle', False), ('paddle', True), ('directml', False),
                              ('python-coreml', False), ('native', False)]:
            with self.subTest(backend=backend, cuda=cuda), patch.dict(os.environ, {
                    'VSE_OCR_BACKEND': backend, 'VSE_NATIVE_OCR_PROVIDER': 'coreml'}):
                hardware = HardwareAccelerator()
                with patch.object(hardware, 'check_paddle'), patch.object(hardware, 'check_onnx'):
                    hardware.initialize()
                hardware._HardwareAccelerator__cuda = cuda  # Synthetic availability; no runtime import.
                for mode in ('fast', 'auto', 'accurate'):
                    selected = PaddleModelConfig(hardware, language='en', mode=mode)
                    self.assertEqual(selected.TIER, 'medium' if mode == 'accurate' or mode == 'auto' and cuda else 'small')
                hardware.set_enabled(False)
                self.assertEqual(PaddleModelConfig(hardware, language='en', mode='auto').TIER, 'small')

    def test_readmes_do_not_promise_inherent_gpu_accuracy_or_generic_medium_auto(self):
        chinese = (ROOT / 'README.md').read_text(encoding='utf-8')
        english = (ROOT / 'README_en.md').read_text(encoding='utf-8')
        self.assertNotIn('GPU加速后可以获得更高的准确率', chinese)
        self.assertNotIn('GPU下使用精准模型', chinese)
        self.assertNotIn('GPU acceleration for higher accuracy', english)
        self.assertNotIn('precise model on GPU', english)
        usage = (ROOT / 'docs/USAGE.md').read_text(encoding='utf-8')
        self.assertIn('only when Paddle CUDA is enabled and available', usage)
        self.assertIn('CoreML auto remain small', usage)
        self.assertIn('not certification of this new source-install recipe', usage)


if __name__ == '__main__':
    unittest.main()
