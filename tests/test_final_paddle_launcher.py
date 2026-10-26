"""All-model acceptance must run the ZIP interpreter, never the build host."""
from pathlib import Path
from unittest.mock import patch
import pytest

from scripts.bundle_bootstrap import main, run_paddle_model_acceptance


def test_external_fixtures_run_only_final_python_and_included_checker(tmp_path):
    root = tmp_path / 'final app'
    python = root / 'runtime/python'
    fixtures = tmp_path / 'build evidence'
    fixtures.mkdir()
    results = tmp_path / 'results'
    env = {'VSE_OFFLINE': '1'}
    with patch('scripts.bundle_bootstrap.subprocess.run') as run:
        run_paddle_model_acceptance(root, python, fixtures, results, env)
    args, kwargs = run.call_args
    assert args[0] == [str(python), str(root / 'scripts/check_paddle_models.py'),
                       '--app-root', str(root), '--fixtures', str(fixtures.resolve()),
                       '--report', str(results / 'paddle-models/report.json')]
    assert kwargs == {'check': True, 'env': env, 'timeout': 1800}


def test_testing_resources_are_not_customer_payload(tmp_path):
    fixtures = tmp_path / 'app/fixture'
    fixtures.mkdir(parents=True)
    with patch('scripts.bundle_bootstrap.subprocess.run') as run:
        with pytest.raises(RuntimeError, match='outside the customer package'):
            run_paddle_model_acceptance(tmp_path / 'app', tmp_path / 'python', fixtures, tmp_path / 'results', {})
    run.assert_not_called()


def test_missing_fixtures_never_skip_the_suite(tmp_path):
    with patch('scripts.bundle_bootstrap.subprocess.run') as run:
        with pytest.raises(FileNotFoundError):
            run_paddle_model_acceptance(tmp_path / 'app', tmp_path / 'python', tmp_path / 'missing', tmp_path / 'results', {})
    run.assert_not_called()


def test_fixture_option_is_only_valid_for_verification():
    with pytest.raises(SystemExit) as error:
        main(['--paddle-model-fixtures', 'not-opened'])
    assert error.value.code == 2
