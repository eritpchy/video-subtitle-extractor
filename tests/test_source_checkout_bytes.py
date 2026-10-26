"""Real Git checkout byte contracts, not a claim of observed Windows CI failure."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from scripts.source_acceptance import contract_files
from scripts import check_paddle_models as paddle
from backend.tools.python_coreml_ocr import _source_identity

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which('git')
HEADER = b'# Final source receipts compare these exact checkout bytes across host platforms.\n'
CONTROL = 'unprotected-control.txt'
NESTED_CONTROL = 'unrelated/gui.py'


def attributes():
    # .gitattributes itself may be checked out with native line endings. This
    # normalization is only for parsing policy, never for source hash evidence.
    return (ROOT / '.gitattributes').read_bytes().replace(b'\r\n', b'\n')


def protected_exact(data):
    return {line.split()[0].decode().removeprefix('/') for line in data.splitlines()
            if line and not line.startswith(b'#') and line.split()[1:] == [b'-text']}


def report_source_hashes():
    # These are the actual raw module maps consumed in ORT request records and
    # Paddle production_modules, not a second manually maintained allowlist.
    result = _source_identity()
    result.update({name.replace('.', '/') + '.py': paddle.sha256(ROOT / (name.replace('.', '/') + '.py'))
                   for name in paddle.PRODUCTION_MODULES})
    return result


def test_independent_report_source_hashes_are_inside_the_checkout_contract():
    assert set(report_source_hashes()) <= set(contract_files())
    assert 'scripts/paddle_package_models.py' in contract_files()  # Actual verifier module loaded/hashed by Paddle.


def test_source_byte_attributes_are_narrow_and_cover_the_complete_contract():
    original, added = attributes().split(HEADER)
    names = contract_files()
    assert len(names) == len(set(names))
    rows = [line.split() for line in added.splitlines() if line.strip()]
    assert all(len(row) == 2 and row[1] == b'-text' and row[0].startswith(b'/') for row in rows)
    added_names = [row[0].decode().removeprefix('/') for row in rows]
    assert len(added_names) == len(set(added_names))
    assert set(added_names) == set(names) - protected_exact(original)
    assert all(not any(char in name for char in '*?[]') for name in added_names)
    assert set(names) <= protected_exact(attributes())
    assert CONTROL not in protected_exact(attributes())


@pytest.fixture
def checkout(tmp_path):
    if not GIT:
        pytest.skip('Real Git is required for source checkout byte regression')
    root = tmp_path.resolve()
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args, data=None):
        return subprocess.run([GIT, '-C', str(root), '-c', 'core.autocrlf=true',
            '-c', 'core.eol=crlf', '-c', f'core.attributesFile={os.devnull}', *args],
            input=data, env=env, check=True, capture_output=True, timeout=30).stdout

    def materialize(policy):
        git('init', '--quiet', '--template=')
        files = {name: (ROOT / name).read_bytes() for name in contract_files()}
        files[CONTROL] = b'first\nsecond\n'
        files[NESTED_CONTROL] = b'# Unrelated same-basename negative control\n'
        entries = []
        # Store exact blob bytes without Git add clean filters. Real checkout
        # is the operation under test; it must actually transform the control.
        for name, content in {'.gitattributes': policy, **files}.items():
            digest = git('hash-object', '-w', '--stdin', data=content).strip()
            entries.append(b'100644 ' + digest + b'\t' + name.encode('utf-8') + b'\n')
        git('update-index', '--index-info', data=b''.join(entries))
        git('checkout-index', '--all', '--force')
        assert (root / CONTROL).read_bytes() == b'first\r\nsecond\r\n'
        assert (root / NESTED_CONTROL).read_bytes() == b'# Unrelated same-basename negative control\r\n'
        return root, files, git
    return materialize


def test_real_autocrlf_checkout_preserves_every_source_receipt_sha256(checkout):
    root, originals, git = checkout(attributes())
    for name in contract_files():
        actual = (root / name).read_bytes()
        assert actual == originals[name], name
        assert hashlib.sha256(actual).digest() == hashlib.sha256(originals[name]).digest(), name
    for name, digest in report_source_hashes().items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    fields = git('check-attr', '-z', 'text', '--', *contract_files()).split(b'\0')
    assert fields.pop() == b''
    assert list(zip(fields[0::3], fields[1::3], fields[2::3])) == [
        (name.encode(), b'text', b'unset') for name in contract_files()]


def test_without_source_protection_real_checkout_changes_raw_hashes(checkout):
    old_policy = attributes().split(HEADER)[0]
    root, originals, _ = checkout(old_policy)
    changed = {name for name in contract_files() if hashlib.sha256((root / name).read_bytes()).digest()
               != hashlib.sha256(originals[name]).digest()}
    # These real source types previously had no byte-preserving Git attribute.
    witnesses = {'.github/workflows/python-source.yml', 'scripts/source_acceptance.py',
                 'backend/tools/python_coreml_runtime.json', 'requirements-source-common.txt'}
    assert witnesses <= changed
    assert changed <= set(contract_files()) - protected_exact(old_policy)
    assert all((root / name).read_bytes() == originals[name]
               for name in set(contract_files()) & protected_exact(old_policy))
