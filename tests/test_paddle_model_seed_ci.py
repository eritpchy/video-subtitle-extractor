"""Offline bootstrap trust-root and same-run model-artifact contracts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import paddle_model_seed_ci as ci
from scripts.paddle_package_models import PINNED_SOURCE_SHA256

ROOT = Path(__file__).resolve().parents[1]


class SeedBootstrapCliTests(unittest.TestCase):
    def test_exact_existing_accepted_source_is_distinct_from_current_build(self):
        self.assertEqual(ci.SOURCE_RUN, '40259119763')
        self.assertEqual(ci.SOURCE_ARTIFACT, '12624088363')
        self.assertEqual(ci.SOURCE_SHA, '59749ccc02e369f374c69846b2bde6279084577d')
        self.assertEqual(ci.ARCHIVE_BYTES, 583979746)
        self.assertEqual(ci.ARCHIVE_SHA256, '6318016c3e0e1452e2aa88d1ca1757b0934a02f677d749d69330a91836bc9fc8')
        self.assertEqual(ci.RECEIPT_SHA256, 'dbca5f75e163bba0620741c1cfee96a65b476279c22aeb7fbf0c4337b51c9f41')

    def test_outputs_bind_exact_run_name_and_pins_and_archive_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'outputs'
            self.assertEqual(ci.main(['--github-outputs', str(output)]), 0)
            lines = dict(line.split('=', 1) for line in output.read_text().splitlines())
            self.assertEqual(lines, {'source_run': ci.SOURCE_RUN, 'source_artifact_name': ci.SOURCE_ARTIFACT_NAME,
                'cache_key': 'paddle-original-seed-v1-' + PINNED_SOURCE_SHA256 + '-' + ci.ARCHIVE_SHA256})

    def test_verify_rechecks_all_external_anchors(self):
        with patch.object(ci, 'verify_seed', return_value={'source_sha': ci.SOURCE_SHA}) as verify:
            self.assertEqual(ci.main(['--verify-seed', 'seed']), 0)
        verify.assert_called_once_with(Path('seed'), **ci.ANCHORS)

    def test_missing_or_wrong_size_archive_does_not_invoke_extractor(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ci, 'seed_models') as seed:
            directory = Path(tmp)
            for exists in (False, True):
                if exists:
                    (directory / ci.ARCHIVE).write_bytes(b'bad')
                with self.assertRaisesRegex(RuntimeError, 'no model-origin fallback'):
                    ci.main(['--bootstrap-directory', str(directory), '--output', str(directory / 'output')])
            seed.assert_not_called()
            self.assertFalse((directory / 'output').exists())

    def test_bootstrap_passes_original_sidecars_and_fixed_anchors(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ci, 'ARCHIVE_BYTES', 3), patch.object(ci, 'seed_models', return_value={'source_sha': ci.SOURCE_SHA}) as seed:
            directory = Path(tmp)
            archive = directory / ci.ARCHIVE
            archive.write_bytes(b'123')
            ci.main(['--bootstrap-directory', str(directory), '--output', str(directory / 'new')])
            seed.assert_called_once_with(archive, archive.with_suffix('.zip.sha256'), archive.with_suffix('.zip.acceptance.json'), directory / 'new', **ci.ANCHORS)

    def test_invalid_action_combinations_fail(self):
        for argv in ([], ['--verify-seed', 'seed', '--output', 'new'], ['--bootstrap-directory', 'seed'],
                     ['--github-outputs', 'out', '--output', 'new'], ['--verify-seed', 'x', '--bootstrap-directory', 'y']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                ci.main(argv)


class SeedWorkflowContracts(unittest.TestCase):
    def test_single_reusable_producer_has_no_origin_download_fallback(self):
        workflow = (ROOT / '.github/workflows/paddle-models.yml').read_text()
        self.assertIn('  workflow_call:', workflow)
        self.assertNotIn('  push:', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('actions: read', workflow)
        self.assertIn('name: ${{ steps.identity.outputs.source_artifact_name }}', workflow)
        self.assertIn('run-id: ${{ steps.identity.outputs.source_run }}', workflow)
        self.assertNotIn('restore-keys:', workflow)
        for forbidden in ('continue-on-error:', 'model_download', 'paddle_package_models.py --output', 'urlopen', 'curl ', 'wget '):
            self.assertNotIn(forbidden, workflow)
        self.assertLess(workflow.index('--bootstrap-directory'), workflow.index('--verify-seed'))
        self.assertLess(workflow.index('--verify-seed'), workflow.index('actions/cache/save@v4'))
        self.assertLess(workflow.index('--verify-seed'), workflow.index('actions/upload-artifact@v4'))
        self.assertIn('name: paddle-original-models-${{ github.sha }}', workflow)

    def test_consumers_require_verified_same_run_full_models_offline(self):
        for name, job in (('ci.yml', 'native'), ('release.yml', 'windows-paddle')):
            workflow = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('  original-models:\n    uses: ./.github/workflows/paddle-models.yml', workflow)
            block = workflow.split('  ' + job + ':\n', 1)[1].split('\n  windows-directml:', 1)[0]
            self.assertIn('    needs: original-models', block)
            self.assertIn('name: paddle-original-models-${{ github.sha }}', block)
            self.assertIn('scripts/paddle_model_seed_ci.py --verify-seed build/paddle-seed', block)
            self.assertIn('--paddle-models build/paddle-seed/models', block)
            self.assertNotIn('paddle_package_models.py --output', workflow)
            self.assertNotIn('backend.tools.model_download --tiers', workflow)
        standard = (ROOT / '.github/workflows/ci.yml').read_text()
        self.assertIn('VSE_MODEL_DIR: ${{ github.workspace }}/build/paddle-seed/models', standard)

    def test_final_acceptance_and_complete_matrix_not_replaced_by_seed(self):
        workflow = (ROOT / '.github/workflows/release.yml').read_text()
        self.assertIn('package_release.py --verify dist --smoke', workflow)
        self.assertIn('scripts/verify_portable_environment.py', workflow)
        self.assertIn('scripts/accept_release_archive.py', workflow)
        aggregate = workflow.split('  candidate-set:\n', 1)[1]
        self.assertIn('needs: [native, windows-paddle, windows-directml]', aggregate)
        self.assertNotRegex(aggregate, r'(?m)^    if:')
        self.assertNotIn('continue-on-error:', aggregate)


if __name__ == '__main__':
    unittest.main()
