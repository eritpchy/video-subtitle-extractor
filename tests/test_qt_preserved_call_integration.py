"""Offline same-run wiring; never substitutes an unavailable old container."""
from pathlib import Path
import pytest,re
from scripts import qt_preserved_resources as p

ROOT=Path(__file__).resolve().parents[1]
def load(name):return (ROOT/'.github/workflows'/name).read_text()
def test_active_entry_calls_only_explicit_Git_producer():
    entry=load('qt-overlay-resources.yml')
    assert entry.split('jobs:\n',1)[1]=='  resources:\n    uses: ./.github/workflows/qt-preserved-resource-producer.yml\n'
    assert 'permissions:\n  contents: read\n  actions: read\n' in entry
    assert 'preserved-6cb' not in (ROOT/'.github/workflows/qt-overlay-resources.yml').read_text()

def test_new_upload_keeps_independent_current_run_attempt_name_for_all_consumers():
    expected='qt-overlay-resources-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}'
    producer=load('qt-preserved-resource-producer.yml')
    upload=producer.split('        id: assets\n',1)[1].split('      - name:',1)[0]
    assert '          name: '+expected+'\n' in upload
    assert '      artifact_name: '+expected+'\n' in producer
    for name in ('native-ocr.yml','python-source.yml'):
        doc=load(name)
        downloads=[b for b in re.split(r'(?m)^      - ',doc) if 'uses: actions/download-artifact@v4' in b and 'name: qt-overlay-resources-' in b]
        assert len(downloads)==1 and '          name: '+expected+'\n' in downloads[0]
        assert 'run-id:' not in downloads[0] and 'repository:' not in downloads[0]
    i={'repository':p.REPO,'commit':'a'*40,'run_id':99,'run_attempt':3,'job':'resources','workflow_path':'.github/workflows/release.yml','scope':'GitHub-hosted-producer'}
    assert p.artifact_name(i)=='qt-overlay-resources-'+'a'*40+'-99-3'

@pytest.mark.parametrize('path',['release.yml','qt-overlay-resources.yml','qt-preserved-resource-producer.yml','native-ocr.yml','python-source.yml'])
def test_only_reviewed_original_call_paths_are_accepted(path):
    p.validate_identity({'repository':p.REPO,'commit':'a'*40,'run_id':99,'run_attempt':3,'job':'resources','workflow_path':'.github/workflows/'+path,'scope':'GitHub-hosted-producer'})

def test_main_and_all_model_and_publication_policies_stay_original():
    assert p.contract()['production_integration_allowed'] is False
    producer=(ROOT/'.github/workflows/qt-preserved-resource-producer.yml').read_text()
    assert 'acknowledge' not in producer and 'workflow_dispatch:' in producer and 'workflow_call:' in producer
    assert 'preserved-inputs' in producer and 'persist-credentials: false' in producer
    assert 'actions: write' not in producer and 'contents: write' not in producer

def test_runner_path_is_resolved_in_a_step_not_the_server_evaluated_job_env():
    producer=load('qt-preserved-resource-producer.yml')
    job_env=producer.split('    env:\n',1)[1].split('    steps:\n',1)[0]
    assert 'runner.' not in job_env and 'VSE_QT_PRESERVED_OUTPUT' not in job_env
    initialization=producer.split('      - name: Set private output path after runner allocation\n',1)[1].split('      - name:',1)[0]
    assert 'shell: bash' in initialization
    assert '"$RUNNER_TEMP" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" >> "$GITHUB_ENV"' in initialization
    assert 'VSE_QT_PRESERVED_OUTPUT=%s/qt-preserved-%s-%s\\n' in initialization
    assert 'mkdir' not in initialization  # prepare() must still require a NEW output directory.
