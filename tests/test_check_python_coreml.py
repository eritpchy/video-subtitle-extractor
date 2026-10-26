"""Source checker fail-closed contracts; no synthetic pass is GPU evidence."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_python_coreml as checker


def test_quick_cases_are_real_nonblank_and_cover_all_fifteen_graphs():
    cases = checker.frozen.load_cases(checker.ROOT / 'native/ocr/tests/evidence')
    selected = [cases[i] for i in checker.MODELS]
    assert len(checker.EARLY) == 4 and len(selected) == 15
    assert all(row['operation'] == 'ocr' and row['expected'] != [[], []] for row in selected)
    covered = {name for row in selected for name in (row['det_model'], row['rec_model'])}
    assert covered == checker.frozen.ALL_MODELS
    assert len(cases) == 168


@pytest.mark.parametrize('name', ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'DYLD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES',
                                 'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH'))
def test_checker_rejects_loader_contamination_before_import_or_inference(tmp_path, monkeypatch, name):
    monkeypatch.setenv(name, '/synthetic/injected')
    monkeypatch.setattr(checker.frozen, 'load_cases', lambda _: pytest.fail('must not load cases'))
    report = checker.run(tmp_path / 'models', tmp_path / 'fixtures', 'cpu', 'all', tmp_path / 'report.json')
    assert report['result'] == 'failed' and name in report['error']
    assert not report['cases'] and report['native_worker_used'] is False


def test_cli_propagates_failed_report_not_green(monkeypatch, tmp_path):
    monkeypatch.setattr(checker, 'run', lambda *a: {'result': 'failed', 'error': 'synthetic'})
    arguments = ['--models', str(tmp_path), '--fixtures', str(tmp_path), '--provider', 'coreml', '--case-set', 'early', '--report', str(tmp_path / 'x')]
    assert checker.main(arguments) == 1
    with pytest.raises(SystemExit): checker.main(arguments + ['--provider', 'fake'])


def test_checker_output_cannot_overwrite_immutable_inputs(tmp_path):
    for destination in ('models/report.json', 'fixtures/report.json'):
        with pytest.raises(ValueError, match='outside immutable'):
            checker.run(tmp_path / 'models', tmp_path / 'fixtures', 'cpu', 'all', tmp_path / destination)


@pytest.mark.parametrize('mutation', ('none', 'tamper', 'extra', 'symlink', 'inventory'))
def test_runtime_rehash_independently_checks_bytes_and_inventory(tmp_path, monkeypatch, mutation):
    root = tmp_path / 'installed'; root.mkdir()
    library = root / 'library.so.1'; library.write_bytes(b'official bytes')
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    checkout = tmp_path / 'checkout'; pins = checkout / 'backend/tools/python_coreml_runtime.json'; pins.parent.mkdir(parents=True)
    pins.write_text(json.dumps({'wheels': {'synthetic': {'filename': 'official.whl', 'native_files': {'library.so.1': digest}}}}))
    monkeypatch.setattr(checker, 'ROOT', checkout)
    runtime = {'package_root': str(root), 'wheel_filename': 'official.whl', 'native_files': {'library.so.1': digest}}
    if mutation == 'tamper': library.write_bytes(b'different bytes')
    elif mutation == 'extra': (root / 'extra.so').write_bytes(b'extra')
    elif mutation == 'symlink':
        original = tmp_path / 'real.so'; library.rename(original); library.symlink_to(original)
    elif mutation == 'inventory': runtime['native_files'] = {}
    if mutation == 'none': assert checker.verify_runtime_files(runtime) == runtime['native_files']
    else:
        with pytest.raises(ValueError): checker.verify_runtime_files(runtime)
