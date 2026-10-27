#!/usr/bin/env python3
"""Prepare exact prepublication Qt resources on hosted CI, without compiling Qt.

The original candidate and preserved final assets are separate, explicit input
contracts. Neither falls back to the other. Ordinary source users use the
separate public-release resource path, never this CI-only credentialed helper.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import build_macos_qt_overlay_resources as builder
from scripts import macos_ax_archive as archives
from scripts import macos_qt_overlay as overlay

ARTIFACT = {
    'id': 12624219589,
    'name': 'macos-qt-ax-diagnostic-not-for-release-561de09ed45180362732725d24640862d3fa1ceb',
    'size_in_bytes': 53736255,
    'digest': 'sha256:25bbdcc961ece4f05bed906df085a66bbab86c1007e086013d17b0a21ab40747',
    'expired': False,
}
RUN = {'id': 40260642504, 'head_sha': '561de09ed45180362732725d24640862d3fa1ceb',
       'head_branch': 'fix/macos-accessibility', 'repository_id': 342871390,
       'head_repository_id': 342871390}
ENDPOINT = 'repos/eritpchy/video-subtitle-extractor/actions/artifacts/12624219589'

# The original endpoint returned 404 after expiry on 2026-10-27. Its identity
# above remains unchanged; the following is a DIFFERENT container of the exact
# final assets, produced and verified by the successful 8f attempt 2 job.
PRESERVED_CONTRACT = 'preserved-8f-attempt2'
PRESERVED_ARTIFACT = {
    'id': 12688107623,
    'name': 'qt-overlay-resources-8f08e7f6887a950f75cb7967f1f80e344935ecaa-40391756697-2',
    'size_in_bytes': 53651264,
    'digest': 'sha256:f1e47e5ee2e765dc0661d5ae5288435ec5da181d49f998bdd6e2f263580fef65',
    'expired': False,
}
PRESERVED_RUN = {'id': 40391756697, 'head_sha': '8f08e7f6887a950f75cb7967f1f80e344935ecaa',
                 'head_branch': 'fix/restore-release-matrix', 'repository_id': 342871390,
                 'head_repository_id': 342871390}
PRESERVED_ATTEMPT = 2
PRESERVED_JOB = 121842501324
PRESERVED_ENDPOINT = 'repos/eritpchy/video-subtitle-extractor/actions/artifacts/12688107623'
PRESERVED_RUN_ENDPOINT = 'repos/eritpchy/video-subtitle-extractor/actions/runs/40391756697/attempts/2'
PRESERVED_JOB_ENDPOINT = 'repos/eritpchy/video-subtitle-extractor/actions/jobs/121842501324'
PRESERVED_ASSETS = {
    'resource': {'name': 'vse-qt6.12.0-macos-arm64-ax772484-ps6.zip', 'bytes': 2547078,
                 'sha256': '9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441'},
    'source_resource': {'name': 'vse-qtbase6.12.0-ax772484-ps6-source.tar.xz', 'bytes': 51103812,
                        'sha256': '5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd'},
}


def _exact_fields(value: dict, expected: dict, label: str) -> None:
    if not isinstance(value, dict) or any(value.get(k) != v or type(value.get(k)) is not type(v)
                                          for k, v in expected.items()):
        raise RuntimeError(f'Fixed preserved input {label} identity mismatch')


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def validate_preserved_metadata(metadata: dict) -> None:
    sha = PRESERVED_RUN.get('head_sha')
    if (not isinstance(sha, str) or re.fullmatch(r'[0-9a-f]{40}', sha) is None
            or type(PRESERVED_RUN.get('id')) is not int or PRESERVED_RUN['id'] <= 0
            or type(PRESERVED_ATTEMPT) is not int or PRESERVED_ATTEMPT <= 0
            or PRESERVED_ARTIFACT['name'] != f'qt-overlay-resources-{sha}-{PRESERVED_RUN["id"]}-{PRESERVED_ATTEMPT}'):
        raise RuntimeError('Preserved source pin must be a complete matching 40-hex SHA/run/attempt')
    _exact_fields(metadata, PRESERVED_ARTIFACT, 'artifact')
    _exact_fields(metadata.get('workflow_run'), PRESERVED_RUN, 'workflow')
    try:
        expires = datetime.fromisoformat(metadata['expires_at'].replace('Z', '+00:00'))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise RuntimeError('Preserved artifact expiry is missing or invalid') from error
    if expires.tzinfo is None or expires <= utc_now():
        raise RuntimeError('Preserved artifact is expired or has no timezone')


def validate_preserved_origin(run: dict, job: dict) -> None:
    _exact_fields(run, {'id': PRESERVED_RUN['id'], 'head_sha': PRESERVED_RUN['head_sha'],
                       'head_branch': PRESERVED_RUN['head_branch'], 'run_attempt': PRESERVED_ATTEMPT,
                       'status': 'completed', 'conclusion': 'success'}, 'attempt')
    _exact_fields(job, {'id': PRESERVED_JOB, 'run_id': PRESERVED_RUN['id'],
                       'run_attempt': PRESERVED_ATTEMPT, 'head_sha': PRESERVED_RUN['head_sha'],
                       'status': 'completed', 'conclusion': 'success',
                       'name': 'qt-overlay-resources / resources'}, 'producer job')


def current_ci_identity() -> dict:
    sha, run, attempt = (os.environ.get(k, '') for k in ('GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))
    if (re.fullmatch(r'[0-9a-f]{40}', sha) is None or re.fullmatch(r'[1-9][0-9]*', run) is None
            or re.fullmatch(r'[1-9][0-9]*', attempt) is None
            or os.environ.get('GITHUB_REPOSITORY') != 'eritpchy/video-subtitle-extractor'):
        raise RuntimeError('Actual consumer SHA/run/attempt/repository is required')
    return {'repository': os.environ['GITHUB_REPOSITORY'], 'commit': sha,
            'run_id': int(run), 'run_attempt': int(attempt)}


def require_hosted_ci() -> None:
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and os.environ.get('RUNNER_OS') in ('Linux', 'macOS')):
        raise RuntimeError('Resource preparation is limited to GitHub-hosted Linux/macOS CI')
    if not os.environ.get('GH_TOKEN'):
        raise RuntimeError('The existing job must provide GH_TOKEN; no credential fallback is used')


def validate_metadata(metadata: dict) -> None:
    if (not isinstance(RUN.get('head_sha'), str)
            or re.fullmatch(r'[0-9a-f]{40}', RUN['head_sha']) is None
            or not ARTIFACT['name'].endswith('-' + RUN['head_sha'])):
        raise RuntimeError('Fixed source pin must be a complete matching 40-hex Git SHA')
    for name, expected in ARTIFACT.items():
        if metadata.get(name) != expected or type(metadata.get(name)) is not type(expected):
            raise RuntimeError(f'Fixed artifact metadata mismatch: {name}')
    run = metadata.get('workflow_run')
    if not isinstance(run, dict):
        raise RuntimeError('Fixed artifact workflow run is missing')
    for name, expected in RUN.items():
        if run.get(name) != expected or type(run.get(name)) is not type(expected):
            raise RuntimeError(f'Fixed artifact workflow identity mismatch: {name}')


def github_to_file(endpoint: str, destination: Path, error_log: Path) -> None:
    """Binary stdout goes straight to a fresh file, never captured into memory."""
    if endpoint not in (ENDPOINT, ENDPOINT + '/zip', PRESERVED_ENDPOINT, PRESERVED_ENDPOINT + '/zip',
                        PRESERVED_RUN_ENDPOINT, PRESERVED_JOB_ENDPOINT):
        raise RuntimeError('No alternate artifact endpoint is accepted')
    env = os.environ.copy()
    env.pop('GH_DEBUG', None)
    env['GH_HOST'] = 'github.com'
    env['GH_PROMPT_DISABLED'] = '1'
    command = ['gh', 'api', '--hostname', 'github.com', endpoint]
    with destination.open('xb') as output, error_log.open('xb') as errors:
        # Authentication remains only in the job environment. Do not log env,
        # HTTP headers, bearer tokens, or authenticated redirect locations.
        result = subprocess.run(command, stdout=output, stderr=errors, env=env,
                                check=False, timeout=600)
    if result.returncode:
        raise RuntimeError(f'Pinned artifact request failed (exit {result.returncode}); original evidence retained')


def prepare_original(output: Path) -> dict:
    require_hosted_ci()
    if output.exists() or output.is_symlink():
        raise RuntimeError('Resource output directory must be new')
    output.mkdir(parents=True)
    reports, inputs = output / 'reports', output / 'inputs'
    reports.mkdir(); inputs.mkdir()
    try:
        policy = overlay.load_policy()
        metadata_path = reports / 'artifact-metadata.json'
        github_to_file(ENDPOINT, metadata_path, reports / 'metadata.stderr.log')
        metadata = overlay.read_json(metadata_path)
        validate_metadata(metadata)
        archive = inputs / 'qt-candidate-artifact.zip'
        github_to_file(ENDPOINT + '/zip', archive, reports / 'download.stderr.log')
        digest = ARTIFACT['digest'].split(':', 1)[1]
        overlay.check_file(archive, digest, ARTIFACT['size_in_bytes'])
        overlay._atomic_json(reports / 'verified-input.json', {
            'artifact': metadata, 'sha256': digest, 'bytes': archive.stat().st_size,
            'metadata_sha256': overlay.sha256(metadata_path)})
        extracted = inputs / 'artifact'
        archives.extract_archive(archive, extracted, require_single_root=False,
                                 max_entries=3000, max_uncompressed_bytes=100 * 1024 * 1024)
        snapshot = archives.inventory(extracted)
        overlay._atomic_json(reports / 'extracted-input-inventory.json', snapshot)
        candidate = extracted / 'build/macos-qt-ax/candidate'
        rebuild = extracted / 'build/macos-qt-ax/rebuild-inputs'
        # The original candidate/source/patch/build-input hashes are independently
        # rechecked by the builder. No arbitrary URL or fallback is accepted.
        result = builder.build_resources(
            candidate_dir=candidate,
            source_archive=rebuild / 'qtbase-everywhere-src-6.12.0.tar.xz',
            patch_path=rebuild / 'qtbase-772484-ps6.patch',
            historical_root=extracted, output_dir=output / 'assets')
        for result_key, policy_key in (('resource_asset', 'resource'), ('source_asset', 'source_resource')):
            expected = {key: policy[policy_key][key] for key in ('name', 'bytes', 'sha256')}
            if result.get(result_key) != expected:
                raise RuntimeError(f'Rebuilt resource differs from frozen policy: {result_key}')
            overlay.check_file(output / 'assets' / expected['name'], expected['sha256'], expected['bytes'])
        if result.get('resource_manifest_sha256') != policy['resource_manifest_sha256']:
            raise RuntimeError('Rebuilt resource manifest differs from frozen policy')
        asset_names = {policy['resource']['name'], policy['source_resource']['name']}
        sidecars = {name + '.sha256' for name in asset_names} | {'resource-build-receipt.json'}
        for path in (output / 'assets').iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Unexpected non-file in prepared assets')
            if path.name in asset_names:
                continue
            if path.name not in sidecars:
                raise RuntimeError('Unexpected resource output; refusing to publish a wider asset set')
            path.rename(reports / path.name)
        if {path.name for path in (output / 'assets').iterdir()} != asset_names:
            raise RuntimeError('Resource output must contain exactly the two frozen assets')
        resource = overlay.prepare_resource(output / 'assets' / policy['resource']['name'],
                                            output / 'verified-resource', policy=policy)
        overlay._validate_resource(resource)
        if archives.inventory(extracted) != snapshot:
            raise RuntimeError('Resource preparation changed its preserved input bytes')
        overlay.check_file(archive, digest, ARTIFACT['size_in_bytes'])
        receipt = {**result, 'status': 'exact-prepublication-resources-verified',
                   'source_artifact': ARTIFACT, 'source_workflow_run': RUN,
                   'production_integration_allowed': False,
                   'scope': 'Resource packaging only; no Mac code execution, final GUI or release approval'}
        overlay._atomic_json(reports / 'resource-preparation.json', receipt)
        return receipt
    except Exception as exc:
        # A failed directory is retained for diagnostics and is never reused as
        # a successful output. Do not retry, overwrite it, or pick a newer run.
        overlay._atomic_json(reports / 'resource-preparation-failed.json', {
            'status': 'failed', 'exception': type(exc).__name__, 'error': str(exc),
            'production_integration_allowed': False,
            'scope': 'Input evidence retained; no alternate source selected'})
        raise


def prepare_preserved(output: Path) -> dict:
    require_hosted_ci()
    consumer = current_ci_identity()
    if output.exists() or output.is_symlink():
        raise RuntimeError('Resource output directory must be new')
    output.mkdir(parents=True)
    reports, inputs = output / 'reports', output / 'inputs'
    reports.mkdir(); inputs.mkdir()
    try:
        policy = overlay.load_policy()
        for key, expected in PRESERVED_ASSETS.items():
            _exact_fields(policy.get(key), expected, 'frozen asset policy')
        if policy.get('production_integration_allowed') is not False:
            raise RuntimeError('Preserved resource cannot promote production admission')
        metadata_path = reports / 'artifact-metadata.json'
        github_to_file(PRESERVED_ENDPOINT, metadata_path, reports / 'metadata.stderr.log')
        metadata = overlay.read_json(metadata_path)
        validate_preserved_metadata(metadata)
        github_to_file(PRESERVED_RUN_ENDPOINT, reports / 'source-attempt.json', reports / 'attempt.stderr.log')
        github_to_file(PRESERVED_JOB_ENDPOINT, reports / 'source-job.json', reports / 'job.stderr.log')
        source_run = overlay.read_json(reports / 'source-attempt.json')
        source_job = overlay.read_json(reports / 'source-job.json')
        validate_preserved_origin(source_run, source_job)
        archive = inputs / 'preserved-assets-artifact.zip'
        github_to_file(PRESERVED_ENDPOINT + '/zip', archive, reports / 'download.stderr.log')
        digest = PRESERVED_ARTIFACT['digest'].split(':', 1)[1]
        overlay.check_file(archive, digest, PRESERVED_ARTIFACT['size_in_bytes'])
        overlay._atomic_json(reports / 'verified-input.json', {
            'input_contract': PRESERVED_CONTRACT, 'artifact': metadata, 'sha256': digest,
            'bytes': archive.stat().st_size, 'source_run_attempt': PRESERVED_ATTEMPT,
            'source_job': PRESERVED_JOB, 'consumer_workflow': consumer,
            'metadata_sha256': overlay.sha256(metadata_path)})
        extracted = inputs / 'artifact'
        archives.extract_archive(archive, extracted, require_single_root=False,
                                 max_entries=2, max_uncompressed_bytes=64 * 1024 * 1024)
        snapshot = archives.inventory(extracted)
        if set(snapshot) != {row['name'] for row in PRESERVED_ASSETS.values()}:
            raise RuntimeError('Preserved container must have exactly the two fixed assets')
        overlay._atomic_json(reports / 'extracted-input-inventory.json', snapshot)
        assets = output / 'assets'; assets.mkdir()
        for expected in PRESERVED_ASSETS.values():
            path = extracted / expected['name']
            overlay.check_file(path, expected['sha256'], expected['bytes'])
            shutil.copyfile(path, assets / expected['name'])
            overlay.check_file(assets / expected['name'], expected['sha256'], expected['bytes'])
        resource = overlay.prepare_resource(assets / policy['resource']['name'],
                                            output / 'verified-resource', policy=policy)
        overlay._validate_resource(resource)
        if archives.inventory(extracted) != snapshot:
            raise RuntimeError('Preserved input changed during resource verification')
        overlay.check_file(archive, digest, PRESERVED_ARTIFACT['size_in_bytes'])
        for expected in PRESERVED_ASSETS.values():
            overlay.check_file(assets / expected['name'], expected['sha256'], expected['bytes'])
        receipt = {'schema': 1, 'input_contract': PRESERVED_CONTRACT,
                   'status': 'exact-prepublication-resources-verified',
                   'resource_asset': PRESERVED_ASSETS['resource'],
                   'source_asset': PRESERVED_ASSETS['source_resource'],
                   'resource_manifest_sha256': policy['resource_manifest_sha256'],
                   'source_artifact': metadata, 'source_workflow_run': PRESERVED_RUN,
                   'source_run_attempt': PRESERVED_ATTEMPT, 'source_producer_job': PRESERVED_JOB,
                   'consumer_workflow': consumer,
                   'historical_original_contract': {
                       'artifact': {k: v for k, v in ARTIFACT.items() if k != 'expired'},
                       'workflow_run': RUN, 'container_supplied_or_requeried': False},
                   'production_integration_allowed': False,
                   'scope': 'Preserved exact asset verification only; not original-container recovery, Mac execution, GUI or release approval'}
        overlay._atomic_json(reports / 'resource-preparation.json', receipt)
        return receipt
    except Exception as exc:
        overlay._atomic_json(reports / 'resource-preparation-failed.json', {
            'status': 'failed', 'input_contract': PRESERVED_CONTRACT,
            'exception': type(exc).__name__, 'error': str(exc), 'consumer_workflow': consumer,
            'production_integration_allowed': False,
            'scope': 'Input evidence retained; no retry or automatic fallback'})
        raise


def prepare(output: Path, *, input_contract: str = 'original-candidate') -> dict:
    if input_contract == 'original-candidate':
        return prepare_original(output)
    if input_contract == PRESERVED_CONTRACT:
        return prepare_preserved(output)
    raise RuntimeError('Unknown explicit Qt resource input contract')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--input-contract', choices=('original-candidate', PRESERVED_CONTRACT),
                        default='original-candidate')
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, input_contract=args.input_contract), indent=2))


if __name__ == '__main__':
    main()
