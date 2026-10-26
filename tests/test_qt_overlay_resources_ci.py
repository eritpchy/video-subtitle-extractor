"""Offline fixed-input CI resource tests; never download or execute Mac code."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scripts import qt_overlay_resources_ci as module


def metadata():
    return {**module.ARTIFACT, 'workflow_run': dict(module.RUN)}


@pytest.mark.parametrize('key', list(module.ARTIFACT))
def test_each_artifact_identity_field_is_required(key):
    value = metadata(); value[key] = True if key == 'expired' else None
    with pytest.raises(RuntimeError): module.validate_metadata(value)


@pytest.mark.parametrize('key', list(module.RUN))
def test_each_source_job_identity_field_is_required(key):
    value = metadata(); value['workflow_run'][key] = None
    with pytest.raises(RuntimeError): module.validate_metadata(value)


def test_metadata_types_cannot_be_coerced():
    value = metadata(); value['expired'] = 0
    with pytest.raises(RuntimeError): module.validate_metadata(value)
    module.validate_metadata(metadata())


def hosted(monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setenv('RUNNER_OS', 'Linux')
    monkeypatch.setenv('GH_TOKEN', 'test-token-must-stay-in-env')


@pytest.mark.parametrize('key,value', [('GITHUB_ACTIONS', 'false'), ('RUNNER_ENVIRONMENT', 'self-hosted'),
                                    ('RUNNER_OS', 'Windows'), ('GH_TOKEN', '')])
def test_only_existing_hosted_ci_token_is_accepted(monkeypatch, key, value):
    hosted(monkeypatch);monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError): module.require_hosted_ci()


def test_download_streams_to_file_without_token_arguments_or_debug(tmp_path, monkeypatch):
    hosted(monkeypatch); monkeypatch.setenv('GH_DEBUG', 'api')
    def run(command, **kwargs):
        assert 'test-token-must-stay-in-env' not in repr(command)
        assert command[:4] == ['gh', 'api', '--hostname', 'github.com']
        assert 'GH_DEBUG' not in kwargs['env']
        assert kwargs['env']['GH_TOKEN'] == 'test-token-must-stay-in-env'
        assert 'capture_output' not in kwargs and 'text' not in kwargs
        kwargs['stdout'].write(b'chunk-one');kwargs['stdout'].write(b'chunk-two')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    output = tmp_path / 'artifact.zip'
    module.github_to_file(module.ENDPOINT + '/zip', output, tmp_path / 'stderr')
    assert output.read_bytes() == b'chunk-onechunk-two'
    with pytest.raises(RuntimeError, match='alternate'):
        module.github_to_file('repos/other/artifact', tmp_path / 'bad', tmp_path / 'badlog')


@pytest.fixture
def prepared_inputs(tmp_path, monkeypatch):
    hosted(monkeypatch)
    archive = tmp_path / 'input.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('build/macos-qt-ax/candidate/candidate.json', '{}')
        output.writestr('build/macos-qt-ax/rebuild-inputs/qtbase-everywhere-src-6.12.0.tar.xz', b'source')
        output.writestr('build/macos-qt-ax/rebuild-inputs/qtbase-772484-ps6.patch', b'patch')
    monkeypatch.setitem(module.ARTIFACT, 'size_in_bytes', archive.stat().st_size)
    monkeypatch.setitem(module.ARTIFACT, 'digest', 'sha256:' + module.overlay.sha256(archive))
    def download(endpoint, destination, error_log):
        error_log.write_text('')
        destination.write_bytes(json.dumps(metadata()).encode() if endpoint == module.ENDPOINT else archive.read_bytes())
    monkeypatch.setattr(module, 'github_to_file', download)
    small, source = b'prepared-small-resource', b'prepared-complete-source'
    def identity(name, value):
        import hashlib
        return {'name': name, 'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
    policy = {'resource': identity('small.zip', small), 'source_resource': identity('source.tar.xz', source),
              'resource_manifest_sha256': 'a' * 64, 'production_integration_allowed': False}
    monkeypatch.setattr(module.overlay, 'load_policy', lambda: copy.deepcopy(policy))
    def build(**kwargs):
        output = kwargs['output_dir'];output.mkdir()
        assert kwargs['candidate_dir'].is_dir()
        (output / 'small.zip').write_bytes(small);(output / 'source.tar.xz').write_bytes(source)
        return {'resource_asset': policy['resource'], 'source_asset': policy['source_resource'],
                'resource_manifest_sha256': 'a' * 64, 'production_integration_allowed': False}
    monkeypatch.setattr(module.builder, 'build_resources', build)
    monkeypatch.setattr(module.overlay, 'prepare_resource', lambda *args, **kwargs: object())
    monkeypatch.setattr(module.overlay, '_validate_resource', lambda _: {})
    return archive, policy


def test_exact_resource_pipeline_preserves_evidence_and_scopes(prepared_inputs, tmp_path):
    output = tmp_path / 'result'
    result = module.prepare(output)
    assert result['status'] == 'exact-prepublication-resources-verified'
    assert result['production_integration_allowed'] is False
    assert (output / 'reports/artifact-metadata.json').is_file()
    assert (output / 'reports/extracted-input-inventory.json').is_file()
    assert (output / 'inputs/qt-candidate-artifact.zip').is_file()
    assert (output / 'assets/small.zip').is_file()
    with pytest.raises(RuntimeError, match='must be new'):
        module.prepare(output)


@pytest.mark.parametrize('failure', ['metadata', 'hash', 'size', 'zip-escape', 'resource-policy', 'source-policy', 'manifest-policy'])
def test_failures_preserve_original_inputs_and_never_fallback(prepared_inputs, tmp_path, monkeypatch, failure):
    archive, policy = prepared_inputs
    original_download = module.github_to_file
    calls = []
    def download(endpoint, destination, error_log):
        calls.append(endpoint)
        original_download(endpoint, destination, error_log)
        if failure == 'metadata' and endpoint == module.ENDPOINT:
            value = json.loads(destination.read_text());value['expired'] = True
            destination.write_text(json.dumps(value))
    monkeypatch.setattr(module, 'github_to_file', download)
    if failure == 'hash': monkeypatch.setitem(module.ARTIFACT, 'digest', 'sha256:' + '0' * 64)
    if failure == 'size': monkeypatch.setitem(module.ARTIFACT, 'size_in_bytes', archive.stat().st_size + 1)
    if failure == 'zip-escape':
        with zipfile.ZipFile(archive, 'w') as output: output.writestr('../escaped.txt', b'unsafe')
        monkeypatch.setitem(module.ARTIFACT, 'digest', 'sha256:' + module.overlay.sha256(archive))
        monkeypatch.setitem(module.ARTIFACT, 'size_in_bytes', archive.stat().st_size)
    if failure in ('resource-policy', 'source-policy', 'manifest-policy'):
        original_build = module.builder.build_resources
        def build(**kwargs):
            result = copy.deepcopy(original_build(**kwargs))
            if failure == 'manifest-policy': result['resource_manifest_sha256'] = 'b' * 64
            else: result['resource_asset' if failure == 'resource-policy' else 'source_asset']['sha256'] = 'b' * 64
            return result
        monkeypatch.setattr(module.builder, 'build_resources', build)
    output = tmp_path / 'failed'
    with pytest.raises((RuntimeError, ValueError)):
        module.prepare(output)
    assert (output / 'reports/resource-preparation-failed.json').is_file()
    assert not (output / 'reports/resource-preparation.json').exists()
    assert len(calls) == (1 if failure == 'metadata' else 2)
    assert not (output / 'inputs/escaped.txt').exists()
    assert not (tmp_path / 'escaped.txt').exists()


def test_builder_sidecars_are_reports_not_published_assets(prepared_inputs, tmp_path, monkeypatch):
    build = module.builder.build_resources
    def with_sidecars(**kwargs):
        result = build(**kwargs)
        for name in ['small.zip.sha256', 'source.tar.xz.sha256', 'resource-build-receipt.json']:
            (kwargs['output_dir'] / name).write_text('preserved build report')
        return result
    monkeypatch.setattr(module.builder, 'build_resources', with_sidecars)
    output = tmp_path / 'result';module.prepare(output)
    assert {p.name for p in (output / 'assets').iterdir()} == {'small.zip', 'source.tar.xz'}
    assert (output / 'reports/resource-build-receipt.json').read_text() == 'preserved build report'


@pytest.mark.parametrize('kind', ['extra-asset', 'input-mutation'])
def test_extra_assets_and_mutated_inputs_block_success(prepared_inputs, tmp_path, monkeypatch, kind):
    build = module.builder.build_resources
    def invalid(**kwargs):
        result = build(**kwargs)
        if kind == 'extra-asset': (kwargs['output_dir'] / 'unreviewed.zip').write_bytes(b'extra')
        else: (kwargs['candidate_dir'] / 'candidate.json').write_text('changed')
        return result
    monkeypatch.setattr(module.builder, 'build_resources', invalid)
    output = tmp_path / 'result'
    with pytest.raises(RuntimeError): module.prepare(output)
    assert not (output / 'reports/resource-preparation.json').exists()
    assert (output / 'reports/resource-preparation-failed.json').is_file()


def test_gh_error_keeps_partial_bytes_without_retry(tmp_path, monkeypatch):
    hosted(monkeypatch)
    calls = []
    def run(command, **kwargs):
        calls.append(command);kwargs['stdout'].write(b'partial response')
        kwargs['stderr'].write(b'simulated access denied')
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='original evidence retained'):
        module.github_to_file(module.ENDPOINT, tmp_path / 'metadata', tmp_path / 'stderr')
    assert (tmp_path / 'metadata').read_bytes() == b'partial response'
    assert len(calls) == 1
