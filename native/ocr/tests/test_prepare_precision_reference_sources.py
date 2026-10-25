"""Synthetic pinned archives; no network, Paddle imports or conversion tools."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import textwrap

import pytest

import prepare_precision_reference_sources as reference


@pytest.fixture
def sources(tmp_path):
    pins = json.loads((reference.HERE / 'model-source-pins.json').read_text())
    cache = tmp_path / 'archive-cache'
    cache.mkdir()
    for row in pins['models']:
        if row['name'] not in reference.MODEL_NAMES:
            continue
        payloads = {name: (row['name'] + ':' + name).encode() for name in reference.SOURCE_FILES}
        archive = cache / 'temporary.tar'
        with tarfile.open(archive, 'w') as tf:
            for name, data in sorted(payloads.items()):
                member = tarfile.TarInfo(row['name'] + '_infer/' + name)
                member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
        row['archive_sha256'] = reference.digest(archive)
        row['archive_bytes'] = archive.stat().st_size
        row['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
        archive.rename(cache / (row['archive_sha256'] + '.tar'))
    pin_path = tmp_path / 'trusted-pins.json'
    pin_path.write_text(json.dumps(pins))
    return tmp_path / 'source-artifact', cache, pin_path, pins


def test_offline_export_and_verify_are_independent_of_converter(sources, monkeypatch):
    output, cache, pins, _ = sources
    monkeypatch.setattr('urllib.request.urlopen', lambda *_a, **_k: pytest.fail('network forbidden'))
    manifest = reference.prepare(output, cache, offline=True, pins_path=pins)
    assert reference.verify(output, pins) == manifest
    assert set(p.name for p in output.iterdir()) == {*reference.MODEL_NAMES, reference.MANIFEST}
    assert len(list(output.glob('*/inference.*'))) == 9
    assert not list(output.glob('**/*.onnx'))


def test_existing_output_is_never_replaced(sources):
    output, cache, pins, _ = sources
    output.mkdir()
    with pytest.raises(FileExistsError):
        reference.prepare(output, cache, True, pins)


def test_offline_miss_fails_without_network_or_partial_artifact(sources, monkeypatch):
    output, cache, pins, _ = sources
    for path in cache.iterdir():
        path.unlink()
    monkeypatch.setattr('urllib.request.urlopen', lambda *_a, **_k: pytest.fail('network forbidden'))
    with pytest.raises(FileNotFoundError):
        reference.prepare(output, cache, True, pins)
    assert not output.exists()
    assert not list(output.parent.glob('.precision-sources-*'))


def test_cache_hit_corruption_fails_closed_without_download(sources, monkeypatch):
    output, cache, pins, _ = sources
    next(cache.iterdir()).write_bytes(b'corrupt')
    monkeypatch.setattr('urllib.request.urlopen', lambda *_a, **_k: pytest.fail('no replacement download'))
    with pytest.raises(ValueError, match='Corrupt reference archive'):
        reference.prepare(output, cache, False, pins)
    assert not output.exists()


def test_online_path_uses_existing_pinned_fetch_api(sources, monkeypatch):
    output, cache, pins, _ = sources
    original = reference.get_archive
    seen = []
    def fetch(row, observed_cache, offline):
        seen.append((row['name'], observed_cache, offline))
        return original(row, observed_cache, True)
    monkeypatch.setattr(reference, 'get_archive', fetch)
    reference.prepare(output, cache, False, pins)
    assert seen == [(name, cache, False) for name in reference.MODEL_NAMES]


@pytest.mark.parametrize('kind', ['source', 'manifest', 'extra-model', 'extra-source'])
def test_artifact_tampering_fails_against_trusted_pins(sources, kind):
    output, cache, pins, _ = sources
    reference.prepare(output, cache, True, pins)
    model = output / reference.MODEL_NAMES[0]
    if kind == 'source':
        (model / 'inference.json').write_bytes(b'changed')
    elif kind == 'manifest':
        manifest = json.loads((output / reference.MANIFEST).read_text())
        manifest['models'][0]['files']['inference.json'] = '0' * 64
        (output / reference.MANIFEST).write_bytes(reference.canonical(manifest))
    elif kind == 'extra-model':
        (output / 'unapproved_model').mkdir()
    else:
        (model / 'training.py').write_text('not a runtime input')
    with pytest.raises(ValueError):
        reference.verify(output, pins)


@pytest.mark.parametrize('kind', ['source', 'model', 'root', 'cache', 'dangling-cache'])
def test_symlinks_are_rejected(sources, kind, tmp_path):
    output, cache, pins, _ = sources
    if kind in ('source', 'model', 'root'):
        reference.prepare(output, cache, True, pins)
        path = output if kind == 'root' else output / reference.MODEL_NAMES[0]
        if kind == 'source':
            path /= 'inference.json'
        moved = tmp_path / ('moved-' + path.name)
        path.rename(moved)
        try:
            path.symlink_to(moved, target_is_directory=moved.is_dir())
        except (OSError, NotImplementedError) as error:
            pytest.skip('test host cannot create symlinks: ' + str(error))
        with pytest.raises(ValueError):
            reference.verify(output, pins)
    else:
        path = next(cache.iterdir()) if kind == 'dangling-cache' else cache
        moved = tmp_path / ('moved-' + path.name)
        path.rename(moved)
        target = tmp_path / 'absent' if kind == 'dangling-cache' else moved
        try:
            path.symlink_to(target, target_is_directory=kind == 'cache')
        except (OSError, NotImplementedError) as error:
            pytest.skip('test host cannot create symlinks: ' + str(error))
        with pytest.raises(ValueError):
            reference.prepare(output, cache, True, pins)


def test_archive_size_pin_is_checked_on_cache_hits(sources):
    output, cache, pins, values = sources
    next(row for row in values['models'] if row['name'] == reference.MODEL_NAMES[0])['archive_bytes'] += 1
    pins.write_text(json.dumps(values))
    with pytest.raises(ValueError, match='archive hash/size/path'):
        reference.prepare(output, cache, True, pins)
    assert not output.exists()


def test_archive_traversal_is_rejected_even_if_archive_hash_matches(sources):
    output, cache, pins, values = sources
    row = next(row for row in values['models'] if row['name'] == reference.MODEL_NAMES[0])
    archive = cache / 'malicious.tar'
    with tarfile.open(archive, 'w') as tf:
        member = tarfile.TarInfo('../inference.json')
        member.size = 2
        tf.addfile(member, io.BytesIO(b'{}'))
    row['archive_sha256'] = reference.digest(archive)
    row['archive_bytes'] = archive.stat().st_size
    archive.rename(cache / (row['archive_sha256'] + '.tar'))
    # Keep the independent cache exact after replacing this fixture's pin.
    permitted = {entry['archive_sha256'] + '.tar' for entry in values['models']
                 if entry['name'] in reference.MODEL_NAMES}
    for path in cache.iterdir():
        if path.name not in permitted:
            path.unlink()
    pins.write_text(json.dumps(values))
    with pytest.raises(ValueError, match='Unsafe source archive member'):
        reference.prepare(output, cache, True, pins)
    assert not output.exists()


def test_cli_imports_without_site_packages_and_accepts_no_artifact_pins():
    script = reference.HERE / 'prepare_precision_reference_sources.py'
    result = subprocess.run([sys.executable, '-S', str(script), '--help'], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert '--verify' in result.stdout
    assert '--pins' not in result.stdout


def test_missing_seed_is_allowed_with_verified_independent_cache(sources):
    output, cache, pins, _ = sources
    reference.prepare(output, cache, True, pins, seed_cache=output.parent / 'not-created')
    reference.verify(output, pins)


def test_seed_copies_only_required_archives_without_network(sources, monkeypatch):
    output, seed, pins, _ = sources
    (seed / 'unrelated-conversion-cache-file').write_bytes(b'not copied')
    cache = output.parent / 'independent-cache'
    monkeypatch.setattr('urllib.request.urlopen', lambda *_a, **_k: pytest.fail('network forbidden'))
    reference.prepare(output, cache, True, pins, seed_cache=seed)
    assert len(list(cache.iterdir())) == 3
    assert all(path.suffix == '.tar' for path in cache.iterdir())
    assert (seed / 'unrelated-conversion-cache-file').is_file()
    reference.verify(output, pins)


def test_corrupt_seed_is_not_ignored_even_with_good_cache(sources):
    output, cache, pins, _ = sources
    seed = output.parent / 'seed'
    seed.mkdir()
    (seed / next(cache.iterdir()).name).write_bytes(b'corrupt seed')
    with pytest.raises(ValueError, match='refusing fallback'):
        reference.prepare(output, cache, True, pins, seed_cache=seed)
    assert not output.exists()


def test_corrupt_cache_is_not_replaced_from_good_seed(sources):
    output, cache, pins, _ = sources
    seed = output.parent / 'seed'
    seed.mkdir()
    original = next(cache.iterdir())
    (seed / original.name).write_bytes(original.read_bytes())
    original.write_bytes(b'corrupt hit')
    with pytest.raises(ValueError, match='refusing fallback'):
        reference.prepare(output, cache, True, pins, seed_cache=seed)
    assert original.read_bytes() == b'corrupt hit'


def test_unrelated_archive_in_independent_cache_is_rejected(sources):
    output, cache, pins, _ = sources
    (cache / 'unrelated.tar').write_bytes(b'not an allowed source')
    with pytest.raises(ValueError, match='only the three pinned archives'):
        reference.prepare(output, cache, True, pins)


def _workflow_script(step_name):
    workflow = (reference.HERE.parents[1] / '.github/workflows/native-ocr.yml').read_text()
    block = workflow.split('      - name: ' + step_name + '\n', 1)[1].split('\n      - ', 1)[0]
    return textwrap.dedent(block.split('          script: |\n', 1)[1])


def _run_workflow_script(script, replies, env=None):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable for executing GitHub Script unit checks')
    payload = json.dumps({'script': script, 'replies': replies, 'env': env or {}})
    harness = r'''
const test = JSON.parse(process.argv[1]);
const records = [], outputs = {}, failures = [], requests = [];
let now = 0, next = 0;
class FakeDate extends Date { static now() { return now; } }
const fs = {mkdirSync() {}, existsSync() { return false; },
  writeFileSync(path, data) { records.push({path, data: JSON.parse(data)}); }};
const core = {setOutput(key, value) { outputs[key] = value; },
  setFailed(value) { failures.push(value); }};
const endpoint = () => { throw new Error('endpoint must be called through pagination'); };
const github = {rest: {actions: {listJobsForWorkflowRun: endpoint}},
  async paginate(api, args) {
    if (api !== endpoint) throw new Error('unexpected API');
    requests.push(args);
    const reply = test.replies[Math.min(next++, test.replies.length - 1)];
    if (reply.error) throw new Error(reply.error);
    return reply.jobs;
  }};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const execute = new AsyncFunction('require','core','github','context','Date','setTimeout','process',test.script);
execute(name => { if (name !== 'fs') throw new Error('unexpected import'); return fs; },
  core, github, {runId: 123, repo: {owner: 'owner', repo: 'repo'}}, FakeDate,
  (callback, delay) => { now += delay; callback(); }, {env: test.env})
  .then(() => process.stdout.write(JSON.stringify({records, outputs, failures, requests})))
  .catch(error => { process.stderr.write(String(error.stack)); process.exit(1); });
'''
    result = subprocess.run([node, '-e', harness, payload], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _job(status, conclusion=None):
    return {'id': 8, 'name': 'Pinned original Paddle sources / diagnostics only',
            'status': status, 'conclusion': conclusion}


@pytest.mark.parametrize('reply', [
    {'jobs': [_job('completed', 'failure')]},
    {'jobs': [_job('completed', 'cancelled')]},
    {'error': 'read API denied'},
    {'jobs': [_job('completed', 'success'), _job('completed', 'success')]},
    {'jobs': []},
])
def test_mac_source_wait_fails_explicitly_without_reference_pass(reply):
    script = _workflow_script('Wait read-only for same-run diagnostic source preparation')
    result = _run_workflow_script(script, [reply])
    assert result['records'][0]['data']['status'] == 'pending'
    assert result['records'][-1]['data']['status'] == 'unavailable'
    assert result['records'][-1]['data']['available'] is False
    assert result['outputs']['available'] == 'false'
    assert result['failures']
    assert all(request['filter'] == 'latest' and request['run_id'] == 123 for request in result['requests'])


def test_mac_source_wait_polls_pending_then_observes_success():
    script = _workflow_script('Wait read-only for same-run diagnostic source preparation')
    result = _run_workflow_script(script, [{'jobs': [_job('in_progress')]},
                                         {'jobs': [_job('completed', 'success')]}])
    assert len(result['requests']) == 2
    assert result['outputs']['available'] == 'true'
    assert not result['failures']
    assert result['records'][-1]['data']['source_job_conclusion'] == 'success'


def test_mac_source_wait_accepts_unique_reusable_workflow_job_prefix():
    script = _workflow_script('Wait read-only for same-run diagnostic source preparation')
    job = _job('completed', 'success')
    job['name'] = 'Retained Native CPU matrix / ' + job['name']
    result = _run_workflow_script(script, [{'jobs': [job]}])
    assert result['outputs']['available'] == 'true'
    assert not result['failures']


@pytest.mark.parametrize('download,reference_outcome', [('failure', 'skipped'), ('success', 'failure')])
def test_artifact_or_reference_setup_failure_records_unavailable(download, reference_outcome):
    script = _workflow_script('Record explicit Mac reference availability and failure evidence')
    result = _run_workflow_script(script, [], {'PRECISION_SOURCE_WAIT': 'success',
        'PRECISION_SOURCE_DOWNLOAD': download, 'PRECISION_REFERENCE': reference_outcome})
    report = result['records'][-1]['data']
    assert report['status'] == 'unavailable'
    assert report['available'] is False and report['reference_available'] is False
    assert report['download_outcome'] == download
    assert result['failures']
