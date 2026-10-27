"""Preserved fixed assets remain separate from the unavailable original ZIP.

All network and native-resource preparation in these small fixtures are mocked;
actual independent raw fixtures bind the production metadata pins.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scripts import qt_overlay_resources_ci as module

FIXTURES = Path(__file__).parent / 'fixtures/qt-overlay'


def metadata():
    return json.loads((FIXTURES / 'artifact-12688107623.json').read_bytes())


def origin():
    return json.loads((FIXTURES / 'preserved-8f-origin-identities.json').read_bytes())


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(module, 'utc_now', lambda: datetime(2026, 10, 27, 12, tzinfo=timezone.utc))


def test_independent_actual_origin_matches_separate_full_metadata_contract():
    assert hashlib.sha256((FIXTURES/'artifact-12688107623.json').read_bytes()).hexdigest() == '9325352b1ecfc118522b1577e8be952db7b3cfa423e39a115997104926fbf4ae'
    assert hashlib.sha256((FIXTURES/'preserved-8f-origin-identities.json').read_bytes()).hexdigest() == '7d563f1dd282d505aee8eaba7085f29dc082daf672b1ff76f5451556681ad132'
    module.validate_preserved_metadata(metadata())
    module.validate_preserved_origin(origin()['run'], origin()['job'])
    assert module.PRESERVED_ARTIFACT['id'] == 12688107623
    assert module.PRESERVED_ARTIFACT['size_in_bytes'] == 53651264
    assert module.PRESERVED_ARTIFACT['digest'] == 'sha256:f1e47e5ee2e765dc0661d5ae5288435ec5da181d49f998bdd6e2f263580fef65'
    assert module.PRESERVED_RUN['head_sha'] == '8f08e7f6887a950f75cb7967f1f80e344935ecaa'
    assert module.PRESERVED_ATTEMPT == 2 and module.PRESERVED_JOB == 121842501324
    assert module.ARTIFACT['id'] == 12624219589 and module.ARTIFACT['size_in_bytes'] == 53736255
    assert module.ARTIFACT['digest'] == 'sha256:25bbdcc961ece4f05bed906df085a66bbab86c1007e086013d17b0a21ab40747'
    assert module.PRESERVED_ARTIFACT != module.ARTIFACT
    policy = module.overlay.load_policy()
    assert module.PRESERVED_ASSETS == {
        'resource': {'name': 'vse-qt6.12.0-macos-arm64-ax772484-ps6.zip', 'bytes': 2547078,
                     'sha256': '9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441'},
        'source_resource': {'name': 'vse-qtbase6.12.0-ax772484-ps6-source.tar.xz', 'bytes': 51103812,
                            'sha256': '5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd'},
    }
    for key, expected in module.PRESERVED_ASSETS.items():
        assert {k: policy[key][k] for k in expected} == expected
    assert policy['production_integration_allowed'] is False


@pytest.mark.parametrize('key', list(module.PRESERVED_ARTIFACT))
def test_each_preserved_container_field_is_required_without_coercion(key):
    row = metadata(); row[key] = True if key == 'expired' else None
    with pytest.raises(RuntimeError): module.validate_preserved_metadata(row)


@pytest.mark.parametrize('key', list(module.PRESERVED_RUN))
def test_each_preserved_workflow_field_is_required(key):
    row = metadata(); row['workflow_run'][key] = None
    with pytest.raises(RuntimeError): module.validate_preserved_metadata(row)


@pytest.mark.parametrize('expiry', [None, '', 'invalid', '2026-10-27T11:59:59Z', '2026-10-27T12:00:00Z', '2030-01-01T00:00:00'])
def test_expiry_is_mandatory_aware_and_future(expiry):
    row = metadata(); row['expires_at'] = expiry
    with pytest.raises(RuntimeError): module.validate_preserved_metadata(row)


@pytest.mark.parametrize('bad', ['a'*39, 'a'*41, 'A'*40, None])
def test_malformed_source_pin_is_rejected_even_when_the_fixture_agrees(monkeypatch, bad):
    monkeypatch.setitem(module.PRESERVED_RUN, 'head_sha', bad)
    row = metadata(); row['workflow_run']['head_sha'] = bad
    with pytest.raises(RuntimeError, match='40-hex'): module.validate_preserved_metadata(row)


def test_metadata_boolean_is_not_integer_or_false():
    row = metadata(); row['id'] = True
    with pytest.raises(RuntimeError): module.validate_preserved_metadata(row)
    row = metadata(); row['expired'] = 0
    with pytest.raises(RuntimeError): module.validate_preserved_metadata(row)


@pytest.mark.parametrize('kind,key', [('run', k) for k in ('id','head_sha','head_branch','run_attempt','status','conclusion')]
                         + [('job', k) for k in ('id','run_id','run_attempt','head_sha','status','conclusion','name')])
def test_successful_exact_attempt_and_actual_producer_job_are_mandatory(kind, key):
    row = origin(); row[kind][key] = None
    with pytest.raises(RuntimeError): module.validate_preserved_origin(row['run'], row['job'])


def hosted(monkeypatch):
    for key, value in {'GITHUB_ACTIONS':'true', 'RUNNER_ENVIRONMENT':'github-hosted', 'RUNNER_OS':'Linux',
                       'GH_TOKEN':'test-only-never-an-argument', 'GITHUB_SHA':'a'*40,
                       'GITHUB_RUN_ID':'999001', 'GITHUB_RUN_ATTEMPT':'3',
                       'GITHUB_REPOSITORY':'eritpchy/video-subtitle-extractor'}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('key,value', [('GITHUB_SHA','a'*39),('GITHUB_SHA','A'*40),('GITHUB_RUN_ID','0'),
                                     ('GITHUB_RUN_ATTEMPT','0'),('GITHUB_RUN_ATTEMPT',''),('GITHUB_REPOSITORY','foreign/repository')])
def test_current_consumer_identity_cannot_be_replaced_by_upstream_identity(monkeypatch, key, value):
    hosted(monkeypatch); monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError): module.current_ci_identity()


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    hosted(monkeypatch)
    small, source = b'exact-small-resource', b'exact-complete-source'
    def identity(name, data):
        return {'name':name, 'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
    assets = {'resource':identity('small.zip',small), 'source_resource':identity('complete.tar.xz',source)}
    monkeypatch.setattr(module, 'PRESERVED_ASSETS', copy.deepcopy(assets))
    policy = {**copy.deepcopy(assets), 'production_integration_allowed':False, 'resource_manifest_sha256':'b'*64}
    monkeypatch.setattr(module.overlay, 'load_policy', lambda:copy.deepcopy(policy))
    archive = tmp_path/'transport.zip'
    def repack(members=None):
        with zipfile.ZipFile(archive,'w') as z:
            for name,data in (members or {'small.zip':small,'complete.tar.xz':source}).items(): z.writestr(name,data)
        monkeypatch.setitem(module.PRESERVED_ARTIFACT,'size_in_bytes',archive.stat().st_size)
        monkeypatch.setitem(module.PRESERVED_ARTIFACT,'digest','sha256:'+module.overlay.sha256(archive))
    repack(); calls = []
    rows = {'metadata':metadata(), **{k:origin()[k] for k in ('run','job')}}
    def fetch(endpoint, destination, error_log):
        calls.append(endpoint); error_log.write_bytes(b'')
        if endpoint == module.PRESERVED_ENDPOINT:
            row=copy.deepcopy(rows['metadata'])
            row['size_in_bytes']=module.PRESERVED_ARTIFACT['size_in_bytes'];row['digest']=module.PRESERVED_ARTIFACT['digest']
            destination.write_bytes(json.dumps(row).encode())
        elif endpoint == module.PRESERVED_RUN_ENDPOINT: destination.write_bytes(json.dumps(rows['run']).encode())
        elif endpoint == module.PRESERVED_JOB_ENDPOINT: destination.write_bytes(json.dumps(rows['job']).encode())
        elif endpoint == module.PRESERVED_ENDPOINT+'/zip': destination.write_bytes(archive.read_bytes())
        else: raise AssertionError('No legacy or alternate fallback allowed: '+endpoint)
    monkeypatch.setattr(module,'github_to_file',fetch)
    monkeypatch.setattr(module.builder,'build_resources',lambda **kw:pytest.fail('Preserved assets must not build Qt or rebuild a container'))
    monkeypatch.setattr(module.overlay,'prepare_resource',lambda *a,**kw:SimpleNamespace())
    monkeypatch.setattr(module.overlay,'_validate_resource',lambda resource:{})
    return SimpleNamespace(archive=archive,policy=policy,rows=rows,calls=calls,repack=repack,small=small,source=source)


def test_explicit_preserved_pipeline_retains_input_bytes_and_distinguishes_current_run(inputs, tmp_path):
    out=tmp_path/'verified';result=module.prepare(out,input_contract=module.PRESERVED_CONTRACT)
    assert result['input_contract']==module.PRESERVED_CONTRACT
    assert result['consumer_workflow']=={'repository':'eritpchy/video-subtitle-extractor','commit':'a'*40,'run_id':999001,'run_attempt':3}
    assert result['source_run_attempt']==2 and result['source_producer_job']==121842501324
    assert result['historical_original_contract']['artifact']['digest']==module.ARTIFACT['digest']
    assert result['historical_original_contract']['container_supplied_or_requeried'] is False
    assert result['production_integration_allowed'] is False
    assert {p.name for p in (out/'assets').iterdir()}=={'small.zip','complete.tar.xz'}
    assert (out/'inputs/preserved-assets-artifact.zip').read_bytes()==inputs.archive.read_bytes()
    assert len(inputs.calls)==4 and module.ENDPOINT not in inputs.calls
    assert all((out/'reports'/name).is_file() for name in ('source-attempt.json','source-job.json','verified-input.json','resource-preparation.json'))
    with pytest.raises(RuntimeError,match='must be new'):module.prepare(out,input_contract=module.PRESERVED_CONTRACT)


@pytest.mark.parametrize('failure',['expired','failed-run','wrong-job','outer-hash','outer-size','resource-member',
                                   'source-member','extra-entry','zip-escape','policy','admission','resource-validation',
                                   'input-mutation','output-mutation','outer-mutation'])
def test_invalid_or_mutated_inputs_fail_closed_and_preserve_raw_without_fallback(inputs,tmp_path,monkeypatch,failure):
    if failure=='expired':inputs.rows['metadata']['expired']=True
    if failure=='failed-run':inputs.rows['run']['conclusion']='failure'
    if failure=='wrong-job':inputs.rows['job']['id']=121842501325
    if failure=='outer-hash':monkeypatch.setitem(module.PRESERVED_ARTIFACT,'digest','sha256:'+'0'*64)
    if failure=='outer-size':monkeypatch.setitem(module.PRESERVED_ARTIFACT,'size_in_bytes',inputs.archive.stat().st_size+1)
    if failure=='resource-member':inputs.repack({'small.zip':b'changed','complete.tar.xz':inputs.source})
    if failure=='source-member':inputs.repack({'small.zip':inputs.small,'complete.tar.xz':b'changed'})
    if failure=='extra-entry':inputs.repack({'small.zip':inputs.small,'complete.tar.xz':inputs.source,'extra.txt':b'extra'})
    if failure=='zip-escape':inputs.repack({'../escaped.txt':b'bad'})
    if failure=='policy':inputs.policy['source_resource']['sha256']='0'*64
    if failure=='admission':inputs.policy['production_integration_allowed']=True
    if failure in ('resource-validation','input-mutation','output-mutation','outer-mutation'):
        def verify(resource):
            out=tmp_path/'failed'
            if failure=='resource-validation':raise RuntimeError('Actual resource validation failed')
            target={'input-mutation':out/'inputs/artifact/complete.tar.xz',
                    'output-mutation':out/'assets/complete.tar.xz',
                    'outer-mutation':out/'inputs/preserved-assets-artifact.zip'}[failure]
            target.write_bytes(b'changed')
        monkeypatch.setattr(module.overlay,'_validate_resource',verify)
    out=tmp_path/'failed'
    with pytest.raises((RuntimeError,ValueError)):module.prepare(out,input_contract=module.PRESERVED_CONTRACT)
    assert (out/'reports/resource-preparation-failed.json').is_file()
    assert not (out/'reports/resource-preparation.json').exists()
    assert module.ENDPOINT not in inputs.calls and module.ENDPOINT+'/zip' not in inputs.calls
    if failure in ('expired','failed-run','wrong-job','policy','admission'):
        assert module.PRESERVED_ENDPOINT+'/zip' not in inputs.calls
    assert not (out/'inputs/escaped.txt').exists()


def test_default_contract_is_still_original_and_unknown_contract_has_no_side_effects(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(module,'prepare_original',lambda output:calls.append(output) or {'mode':'original'})
    assert module.prepare(tmp_path/'legacy')=={'mode':'original'}
    with pytest.raises(RuntimeError,match='Unknown explicit'):module.prepare(tmp_path/'unknown',input_contract='latest-or-fallback')
    assert calls==[tmp_path/'legacy'] and not (tmp_path/'unknown').exists()


def test_failed_preserved_download_never_selects_the_legacy_contract(inputs,tmp_path,monkeypatch):
    calls=[]
    def denied(endpoint,destination,error_log):
        calls.append(endpoint);destination.write_bytes(b'partial');error_log.write_text('simulated HTTP403')
        raise RuntimeError('Pinned request failed; original evidence retained')
    monkeypatch.setattr(module,'github_to_file',denied)
    monkeypatch.setattr(module,'prepare_original',lambda output:pytest.fail('No fallback after denied input'))
    out=tmp_path/'denied'
    with pytest.raises(RuntimeError):module.prepare(out,input_contract=module.PRESERVED_CONTRACT)
    assert calls==[module.PRESERVED_ENDPOINT]
    assert (out/'reports/artifact-metadata.json').read_bytes()==b'partial'
    assert (out/'reports/resource-preparation-failed.json').is_file()
