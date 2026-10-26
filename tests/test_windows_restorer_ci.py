"""Contracts only for the isolated restorer diagnostic snapshot, not release."""
from __future__ import annotations
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('restorer_ci', ROOT / 'scripts/run_windows_restorer_ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class DiagnosticContracts(unittest.TestCase):
    def test_only_exact_diagnostic_ref_is_excluded_from_existing_push(self):
        for name in ('ci.yml', 'release.yml'):
            text = (ROOT / '.github/workflows' / name).read_text()
            push = text.split('  push:\n', 1)[1].split('  pull_request:\n', 1)[0]
            self.assertIn("    branches: ['**', '!fix/windows-restorer']\n", push)
            self.assertNotIn('branches-ignore:', text)
            self.assertIn('  pull_request:\n', text)
            self.assertIn('  workflow_dispatch:\n', text)
        release = (ROOT / '.github/workflows/release.yml').read_text()
        aggregate = release.split('  candidate-set:\n', 1)[1]
        self.assertIn('needs: [native, windows-paddle, windows-directml]', aggregate)
        self.assertNotRegex(aggregate, r'(?m)^    if:')
        self.assertNotIn('continue-on-error:', aggregate)

    def test_scoped_diagnostic_triggers_and_real_server_matrix(self):
        text = (ROOT / '.github/workflows/windows-restorer.yml').read_text()
        push = text.split('  push:\n', 1)[1].split('  workflow_dispatch:\n', 1)[0]
        self.assertIn('    branches: [fix/windows-restorer]\n', push)
        paths = ast.literal_eval(push.split('    paths: ', 1)[1].splitlines()[0])
        self.assertEqual(paths, ['.github/workflows/windows-restorer.yml', 'native/windows-restorer/**',
            'scripts/release_volume_transport.py', 'scripts/run_windows_restorer_ci.py',
            'tests/test_release_volume_transport.py', 'tests/test_windows_restorer_ci.py'])
        self.assertIn('os: [windows-2022, windows-2025]', text)
        self.assertIn('fail-fast: false', text)
        self.assertIn('contents: read', text)
        for forbidden in ('contents: write', 'action-gh-release', '--publish', 'self-hosted', 'Bypass', 'Unblock', 'continue-on-error:'):
            self.assertNotIn(forbidden, text)

    def test_never_uploads_executables_parts_or_whole_fixture_tree(self):
        text = (ROOT / '.github/workflows/windows-restorer.yml').read_text()
        uploads = text.split('uses: actions/upload-artifact@v4', 1)[1]
        self.assertNotIn('.exe', uploads)
        self.assertNotIn('build/restorer-native/\n', uploads)
        self.assertIn('if-no-files-found: error', uploads)
        self.assertIn('test-results/restorer-server/', uploads)
        self.assertIn('**/*record.json', uploads)
        self.assertIn('**/fixture-source/', uploads)

    def test_large_zip64_is_mandatory_and_toolchain_is_not_installed(self):
        text = (ROOT / 'scripts/run_windows_restorer_ci.py').read_text()
        self.assertIn("'--large'", text)
        self.assertIn("child.get('large_requested') is not True", text)
        self.assertNotIn('pinned_build_tools(', text)
        self.assertNotIn('export_environment(', text)
        self.assertNotIn('pip', text)
        self.assertIn('tools.bind_selected_toolset(active, selected)', text)
        self.assertIn("actual.product_type != 3", text)


class InstalledToolchainSelection(unittest.TestCase):
    def setup_tools(self, directory, versions=('14.44.35207',)):
        installation = Path(directory) / 'Visual Studio'
        script = installation / 'Common7/Tools/VsDevCmd.bat'
        script.parent.mkdir(parents=True)
        script.write_text('test fixture, never executed')
        for version in versions:
            (installation / 'VC/Tools/MSVC' / version).mkdir(parents=True)
        tools = mock.Mock()
        tools.inspect_toolset.side_effect = lambda path: {'toolset': path.name,
            'compiler': {'version': '19.44.35207.1'}, 'linker': {'version': '14.44.35207.1'},
            'crt': {'version': '14.44.35207.1'}}
        tools.version.side_effect = lambda value: tuple(map(int, value.split('.')))
        tools.initialize_environment.return_value = {'PATH': 'reviewed only'}
        tools.validate_environment.return_value = {'runtime_linkage': 'old MD', 'loader_compatibility': 'old Native'}
        instances = [{'installationPath': str(installation), 'installationVersion': '17.14.0'}]
        return tools, instances, script

    def test_selects_exact_highest_reviewed_installed_then_binds(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, instances, _ = self.setup_tools(directory, ('14.44.35207', '14.44.35209'))
            report = {}
            environment = ci.select_installed(tools, instances, '14.51.36247.0', report)
            self.assertEqual(environment, {'PATH': 'reviewed only'})
            self.assertEqual(report['selected']['toolset'], '14.44.35209')
            tools.bind_selected_toolset.assert_called_once()
            self.assertIn('/MT', report['active']['runtime_linkage'])
            self.assertNotIn('Native', report['active']['loader_compatibility'])

    def test_incompatible_tools_fail_without_initializing_or_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, instances, _ = self.setup_tools(directory)
            tools.check_versions.side_effect = RuntimeError('unreviewed')
            report = {}
            with self.assertRaisesRegex(RuntimeError, 'No reviewed installed'):
                ci.select_installed(tools, instances, '14.51.36247.0', report)
            tools.initialize_environment.assert_not_called()
            self.assertFalse(report['candidates'][0]['compatible'])

    def test_missing_script_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, instances, script = self.setup_tools(directory)
            script.unlink()
            with self.assertRaisesRegex(RuntimeError, 'No reviewed installed'):
                ci.select_installed(tools, instances, '14.51.36247.0', {})
            tools.initialize_environment.assert_not_called()

    def test_environment_identity_failure_is_not_bypassed(self):
        with tempfile.TemporaryDirectory() as directory:
            tools, instances, _ = self.setup_tools(directory)
            tools.bind_selected_toolset.side_effect = RuntimeError('identity differs')
            with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                ci.select_installed(tools, instances, '14.51.36247.0', {})


if __name__ == '__main__':
    unittest.main()
