import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml
import server_focus_probe as probe

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT.parents[1] / '.github/workflows/macos-focus-activation-diagnostic.yml'


class WorkflowTests(unittest.TestCase):
    def workflow(self):
        # YAML 1.1 SafeLoader treats 'on' as boolean; BaseLoader retains Actions keys.
        return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)

    def test_only_exact_diagnostic_ref_and_paths_without_release_writes(self):
        doc = self.workflow()
        self.assertEqual(doc['on']['push']['branches'], ['fix/macos-focus-activation'])
        self.assertEqual(doc['on']['push']['paths'], [
            '.github/workflows/macos-focus-activation-diagnostic.yml', 'diagnostics/macos-focus-activation/**'])
        self.assertIn('workflow_dispatch', doc['on'])
        self.assertEqual(doc['permissions'], {'contents': 'read'})
        self.assertEqual(list(doc['jobs']), ['focus'])
        job = doc['jobs']['focus']
        self.assertEqual(job['runs-on'], 'macos-26')
        self.assertEqual(job['if'], "github.ref == 'refs/heads/fix/macos-focus-activation'")
        text = WORKFLOW.read_text()
        for forbidden in ('secrets.', 'gh release', 'git push', 'workflow_run:', 'activateIgnoring',
                          'setActivationPolicy', 'defaults write', 'sudo ', 'install_videosubfinder',
                          'paddle_model_seed', 'run_ocr', 'cmake '):
            self.assertNotIn(forbidden, text)

    def test_two_separate_checkouts_do_not_patch_subject(self):
        steps = self.workflow()['jobs']['focus']['steps']
        checkouts = [step for step in steps if step.get('uses', '').startswith('actions/checkout@')]
        self.assertEqual(len(checkouts), 2)
        self.assertEqual(checkouts[0]['with']['ref'], '${{ github.sha }}')
        self.assertEqual(checkouts[1]['with']['ref'], probe.SOURCE_SHA)
        self.assertNotEqual(checkouts[0]['with']['path'], checkouts[1]['with']['path'])
        self.assertTrue(all(step['with']['persist-credentials'] == 'false' for step in checkouts))
        self.assertEqual(checkouts[1]['with']['lfs'], 'false')
        self.assertEqual(checkouts[1]['with']['sparse-checkout'].splitlines(), ['/*', '!/backend/models/'])
        self.assertEqual(checkouts[1]['with']['sparse-checkout-cone-mode'], 'false')

    def test_real_sparse_checkout_excludes_only_model_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            keep = ['gui.py', 'requirements.txt', 'backend/config.py', 'backend/tools/constant.py',
                    'backend/interface/en.ini', 'ui/macos_setting_controls.py', 'ui/resource/icon.png',
                    'config/config.json', 'tests/helpers/theme_wait.py', '.gitattributes']
            omitted = ['backend/models/V5/a.pdiparams', 'backend/models/manifest.json']
            for relative in keep + omitted:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'# controlled sparse fixture\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=jason', '-c',
                            'user.email=eritpchy@qq.com', 'commit', '-qm', 'controlled sparse fixture'], check=True)
            head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'])
            subprocess.run(['git', '-C', str(root), 'sparse-checkout', 'set', '--no-cone', '--stdin'],
                           input=b'/*\n!/backend/models/\n', check=True, capture_output=True)
            for relative in keep:
                self.assertEqual((root / relative).read_bytes(), b'# controlled sparse fixture\n')
            for relative in omitted:
                self.assertFalse((root / relative).exists())
            self.assertEqual(subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD']), head)
            self.assertEqual(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain']), b'')

    def test_original_cases_once_each_and_failure_propagation(self):
        steps = self.workflow()['jobs']['focus']['steps']
        runs = [step for step in steps if step.get('id') in ('wait', 'tab')]
        self.assertEqual([step['id'] for step in runs], ['wait', 'tab'])
        for step in runs:
            self.assertEqual(step['if'], "${{ !cancelled() && steps.prepare.outcome == 'success' }}")
            cmd = step['run']
            self.assertEqual(cmd.count('server_focus_probe.py'), 1)
            self.assertEqual(cmd.count('--case ' + step['id']), 1)
            self.assertIn('python -B ', cmd)
            self.assertIn('set -euo pipefail', cmd)
            self.assertIn('pipeline_status=("${PIPESTATUS[@]}")', cmd)
            self.assertIn('result=${pipeline_status[0]}', cmd)
            self.assertIn('exit "$result"', cmd)
            self.assertIn('exit "$tee_result"', cmd)
            self.assertNotIn('continue-on-error', step)
        artifact = steps[-1]
        self.assertEqual(artifact['if'], 'always()')
        self.assertEqual(artifact['with']['if-no-files-found'], 'error')

    def test_all_run_blocks_parse_as_bash_without_execution(self):
        for step in self.workflow()['jobs']['focus']['steps']:
            if 'run' not in step:
                continue
            with self.subTest(step=step['name']), tempfile.NamedTemporaryFile(mode='w') as handle:
                handle.write(step['run'])
                handle.flush()
                subprocess.run(['bash', '-n', handle.name], check=True, capture_output=True)

    def test_constraints_match_raw_observed_install_not_invented_versions(self):
        evidence = json.loads((ROOT / 'observed-dependencies.json').read_text())
        self.assertEqual(evidence['source_commit'], probe.SOURCE_SHA)
        self.assertEqual(evidence['job_id'], 121724040980)
        self.assertEqual(evidence['installed_line_number'], 1059)
        tokens = evidence['installed_line'].split('Successfully installed ', 1)[1].split()
        expected = {token.rsplit('-', 1)[0]: token.rsplit('-', 1)[1] for token in tokens}
        rows = [line for line in (ROOT / 'aeb-installed-constraints.txt').read_text().splitlines()
                if line and not line.startswith('#')]
        actual = dict(row.split('==', 1) for row in rows)
        self.assertEqual(len(rows), 257)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['pyside6'], '6.12.0')
        self.assertEqual(actual['pyobjc-core'], '12.2.2')
        prepare = next(step for step in self.workflow()['jobs']['focus']['steps'] if step.get('id') == 'prepare')
        self.assertNotIn('--only-binary', prepare['run'])
        self.assertNotIn('--no-binary', prepare['run'])
        self.assertIn('/source-aeb/requirements.txt', prepare['run'])
        self.assertNotIn('upgrade', prepare['run'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
