"""Exercise real Git CRLF checkout without weakening frozen OCR byte identities."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts import check_paddle_models as checker
from scripts import paddle_package_models as models


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = 'native/ocr/tests/evidence'
PINS = 'native/ocr/model-source-pins.json'
GOLDEN = 'native/ocr/tests/goldens/smoke.json'
FROZEN_TEXT = (
    PINS, GOLDEN, f'{EVIDENCE}/fixture-manifest.json',
    f'{EVIDENCE}/legacy/conversion-manifest.json',
    f'{EVIDENCE}/legacy/pipeline-comparison.json',
    f'{EVIDENCE}/missing/conversion-manifest.json',
    f'{EVIDENCE}/missing/pipeline-comparison.json',
    f'{EVIDENCE}/detection/direct-standalone-detection-baseline.json',
)
GIT = shutil.which('git')


@unittest.skipUnless(GIT, 'Real Git is required for checkout byte regression')
class FrozenCheckoutByteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='vse-frozen-checkout-')
        self.addCleanup(directory.cleanup)
        self.checkout = Path(directory.name)
        # Do not inherit another worktree/index or user attribute/filter policy.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.files = {name: (ROOT / name).read_bytes() for name in FROZEN_TEXT}
        manifest = json.loads(self.files[f'{EVIDENCE}/fixture-manifest.json'])
        self.files.update({f'{EVIDENCE}/{row["path"]}': (ROOT / EVIDENCE / row['path']).read_bytes()
                           for row in manifest['files']})
        self.files['unprotected-control.txt'] = b'first\nsecond\n'

    def git(self, *arguments, input=None):
        return subprocess.run(
            [GIT, '-C', str(self.checkout), '-c', 'core.autocrlf=true',
             '-c', 'core.eol=crlf', '-c', f'core.attributesFile={os.devnull}', *arguments],
            input=input, env=self.env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30).stdout

    def checkout_bytes(self, attributes):
        self.git('init', '--quiet', '--template=')
        entries = []
        # Seed exact repository blob bytes, not normalized "git add" output.
        # checkout-index is the operation under test and applies real Git attrs.
        for name, data in {'.gitattributes': attributes, **self.files}.items():
            digest = self.git('hash-object', '-w', '--stdin', input=data).strip()
            entries.append(b'100644 ' + digest + b'\t' + name.encode('utf-8') + b'\n')
        self.git('update-index', '--index-info', input=b''.join(entries))
        self.git('checkout-index', '--all', '--force')
        self.assertEqual((self.checkout / 'unprotected-control.txt').read_bytes(),
                         b'first\r\nsecond\r\n', 'The test must actually enable CRLF conversion')

    def test_autocrlf_checkout_preserves_frozen_bytes_and_canonical_cases(self):
        self.checkout_bytes((ROOT / '.gitattributes').read_bytes())
        for name in FROZEN_TEXT:
            with self.subTest(path=name):
                self.assertEqual((self.checkout / name).read_bytes(), self.files[name])
        data, pins = models._read_pins(self.checkout / PINS)
        self.assertEqual(data, self.files[PINS])
        self.assertEqual(len(pins['models']), 15)
        cases = checker.load_cases(self.checkout / EVIDENCE)
        self.assertEqual(len(cases), 168)
        self.assertEqual(checker._case_proof(cases), checker.CASE_PROOF_SHA256)

    def test_unprotected_checkout_reproduces_fail_closed_trust_mismatches(self):
        self.checkout_bytes(b'')
        for name in FROZEN_TEXT:
            with self.subTest(path=name):
                self.assertNotEqual((self.checkout / name).read_bytes(), self.files[name])
        with self.assertRaisesRegex(ValueError, 'fixed trusted SHA256'):
            models._read_pins(self.checkout / PINS)
        with self.assertRaisesRegex(ValueError, 'Frozen fixture/golden trust root mismatch'):
            checker.load_cases(self.checkout / EVIDENCE)


if __name__ == '__main__':
    unittest.main()
