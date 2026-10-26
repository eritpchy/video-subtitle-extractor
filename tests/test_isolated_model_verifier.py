"""Real child-process regression for isolated verification's startup flags.

The tiny module is a bytecode fixture, not model/inference evidence. These
tests run the current host interpreter; only Server CI verifies Windows.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import bundle_bootstrap


PROBE = r'''
import sys
# -I correctly ignores PYTHONPATH. Add only this explicitly owned test module.
sys.path.insert(0, sys.argv[1])
import vse_integrity_fixture as fixture
import json
import os
print(json.dumps({
    'value': fixture.VALUE,
    'module': fixture.__file__,
    'isolated': sys.flags.isolated,
    'ignore_environment': sys.flags.ignore_environment,
    'no_site': sys.flags.no_site,
    'utf8_mode': sys.flags.utf8_mode,
    'dont_write_bytecode_flag': sys.flags.dont_write_bytecode,
    'dont_write_bytecode': sys.dont_write_bytecode,
    'environment_utf8': os.environ.get('PYTHONUTF8'),
    'environment_no_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE'),
}))
'''


class IsolatedModelVerifierTests(unittest.TestCase):
    def probe(self, flags, *, stale_cache=True, utf8_environment='1', no_bytecode_environment='1'):
        with tempfile.TemporaryDirectory(prefix='vse isolated child ') as temporary:
            root = Path(temporary).resolve()
            source = root / 'vse_integrity_fixture.py'
            source.write_bytes(b"VALUE = 'old'\n")
            cache = Path(importlib.util.cache_from_source(str(source)))
            if stale_cache:
                # Always use a known timestamp cache. Changed source size makes
                # it stale even on filesystems with coarse mtime resolution.
                py_compile.compile(str(source), cfile=str(cache), doraise=True,
                                   invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
                before = cache.read_bytes()
            else:
                before = None
            source.write_bytes(b"VALUE = 'current source, not stale cached code'\n")
            environment = {key: value for key, value in os.environ.items()
                           if not key.upper().startswith('PYTHON')}
            environment.update(PYTHONUTF8=utf8_environment,
                               PYTHONDONTWRITEBYTECODE=no_bytecode_environment)
            result = subprocess.run([sys.executable, *flags, '-c', PROBE, str(root)],
                                    env=environment, cwd=root, check=True, capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            report = json.loads(result.stdout)
            self.assertEqual(Path(report['module']).resolve(), source)
            self.assertEqual(report['value'], 'current source, not stale cached code')
            self.assertEqual(report['isolated'], 1)
            self.assertEqual(report['ignore_environment'], 1)
            self.assertEqual(report['no_site'], 1)
            return report, before, cache.read_bytes() if cache.exists() else None

    def verifier_flags(self):
        root = Path(tempfile.gettempdir()) / 'owned final bundle'
        python = root / 'runtime/python'
        with patch.object(bundle_bootstrap.subprocess, 'run') as run:
            bundle_bootstrap.run_paddle_model_integrity(root, python)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(command, [str(python), '-I', '-B', '-X', 'utf8', '-S',
                                   'scripts/paddle_package_models.py', '--verify-only',
                                   str(root / 'backend/models')])
        self.assertIs(run.call_args.kwargs['check'], True)
        return command[1:command.index('scripts/paddle_package_models.py')]

    def test_old_isolated_child_ignores_utf8_environment(self):
        off, _, _ = self.probe(['-I', '-S'], utf8_environment='0')
        on, _, _ = self.probe(['-I', '-S'], utf8_environment='1')
        self.assertEqual(off['environment_utf8'], '0')
        self.assertEqual(on['environment_utf8'], '1')
        # Do not assume a host locale: -I ignores both settings regardless of
        # whether that platform's default UTF-8 mode is on or off.
        self.assertEqual(off['utf8_mode'], on['utf8_mode'])

    def test_old_isolated_child_rewrites_stale_cache_despite_no_bytecode_environment(self):
        report, before, after = self.probe(['-I', '-S'])
        self.assertEqual(report['environment_no_bytecode'], '1')
        self.assertEqual(report['dont_write_bytecode_flag'], 0)
        self.assertIs(report['dont_write_bytecode'], False)
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        self.assertNotEqual(after, before, 'the unsafe launch must reproduce a real pyc rewrite')

    def test_production_verifier_flags_preserve_stale_cache_and_force_utf8(self):
        flags = self.verifier_flags()
        for utf8_environment, no_bytecode_environment in (('1', '1'), ('0', '')):
            with self.subTest(utf8=utf8_environment, no_bytecode=no_bytecode_environment):
                report, before, after = self.probe(flags, utf8_environment=utf8_environment,
                                                  no_bytecode_environment=no_bytecode_environment)
                self.assertEqual(report['utf8_mode'], 1)
                self.assertEqual(report['dont_write_bytecode_flag'], 1)
                self.assertIs(report['dont_write_bytecode'], True)
                self.assertIsNotNone(before)
                self.assertEqual(after, before, 'verification changed manifest-covered pyc bytes')

    def test_production_verifier_flags_create_no_new_bytecode_cache(self):
        report, before, after = self.probe(self.verifier_flags(), stale_cache=False,
                                          utf8_environment='0', no_bytecode_environment='')
        self.assertEqual(report['utf8_mode'], 1)
        self.assertIs(report['dont_write_bytecode'], True)
        self.assertIsNone(before)
        self.assertIsNone(after)


if __name__ == '__main__':
    unittest.main()
