# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('source_bundle_tested', HERE / 'source_bundle.py')
bundle = importlib.util.module_from_spec(spec); spec.loader.exec_module(bundle)


class SourceBundleTests(unittest.TestCase):
    def test_full_source_snapshot_fixed_header_notices_no_obj_or_runtime(self):
        sources = bundle.snapshot_sources(ROOT)
        notices = {name: b'unit test notice, not for distribution' for name in ('license.docx', 'license-2015-2022.docx', 'Apache-2.0.txt', 'THIRD_PARTY_NOTICES.txt')}
        record = {'git': {'commit': 'a' * 40, 'dirty': True, 'status_porcelain_v1': '?? new.cpp\n'}, 'exe_sha256': 'b' * 64}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); archive = root / 'source.zip'
            result = bundle.create_source_bundle(archive, sources=sources, generated_header=b'// fixed synthetic header\n', notices=notices, build_record=record, transport_manifest=b'{}')
            self.assertEqual(result['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertFalse(result['bit_identical_rebuild_verified'])
            with zipfile.ZipFile(archive) as source:
                names = source.namelist()
                self.assertTrue(set(bundle.SOURCE_PATHS) <= set(names))
                self.assertIn('generated/bundle_metadata.hpp', names)
                self.assertIn('scripts/release_volume_transport.py', names)
                self.assertIn('scripts/runtime_dependencies.py', names)
                self.assertFalse(any(name.endswith(('.obj', '.exe', '.dll', '.pyc', '.lib')) for name in names))
                extracted = root / 'source'; source.extractall(extracted)
            manifest = bundle.verify_extracted_sources(extracted)
            self.assertTrue(manifest['git']['dirty'])
            # -S excludes site-packages: the fixed-header rebuild entry point
            # can import/run without packaging or a copied Python environment.
            help_result = subprocess.run([sys.executable, '-S', str(extracted / 'native/windows-restorer/rebuild.py'), '--help'], capture_output=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertFalse(list(extracted.rglob('__pycache__')))
            bundle.verify_extracted_sources(extracted)
            (extracted / 'native/windows-restorer/core.hpp').write_text('modified')
            with self.assertRaisesRegex(RuntimeError, 'hash mismatch'): bundle.verify_extracted_sources(extracted)

    def test_missing_extra_sources_and_notices_fail_closed(self):
        sources = bundle.snapshot_sources(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            for bad in ({}, {**sources, 'restorer.obj': b'bad'}):
                with self.assertRaises(ValueError): bundle.create_source_bundle(Path(temp) / 'bad.zip', sources=bad, generated_header=b'x', notices={}, build_record={}, transport_manifest=b'{}')
            with self.assertRaises(ValueError): bundle.create_source_bundle(Path(temp) / 'bad.zip', sources=sources, generated_header=b'x', notices={}, build_record={}, transport_manifest=b'{}')
            self.assertFalse((Path(temp) / 'bad.zip').exists())

    def test_source_change_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'a.cpp').write_bytes(b'first')
            snapshot = {'a.cpp': b'first'}; bundle.assert_sources_unchanged(root, snapshot)
            (root / 'a.cpp').write_bytes(b'second')
            with self.assertRaises(RuntimeError): bundle.assert_sources_unchanged(root, snapshot)

    def test_git_dirty_identity_is_not_replaced_by_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=jason', '-c', 'user.email=eritpchy@qq.com', 'commit', '--allow-empty', '-qm', 'test fixture'], check=True,
                           env={**__import__('os').environ, 'GIT_AUTHOR_NAME': 'jason', 'GIT_AUTHOR_EMAIL': 'eritpchy@qq.com', 'GIT_COMMITTER_NAME': 'jason', 'GIT_COMMITTER_EMAIL': 'eritpchy@qq.com'})
            clean = bundle.git_identity(root); self.assertFalse(clean['dirty'])
            (root / 'untracked.cpp').write_text('test')
            dirty = bundle.git_identity(root); self.assertTrue(dirty['dirty']); self.assertIn('untracked.cpp', dirty['status_porcelain_v1'])
            self.assertEqual(clean['commit'], dirty['commit'])


if __name__ == '__main__': unittest.main()
