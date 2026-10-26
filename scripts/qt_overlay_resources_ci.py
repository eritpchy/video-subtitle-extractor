#!/usr/bin/env python3
"""Prepare exact prepublication Qt resources on hosted CI, without compiling Qt.

Only one reviewed Actions artifact is accepted. Ordinary source users use the
separate public-release resource path, never this CI-only credentialed helper.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
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
RUN = {'id': 40260642504, 'head_sha': '561de09ed45180362725d24640862d3fa1ceb',
       'head_branch': 'fix/macos-accessibility', 'repository_id': 342871390,
       'head_repository_id': 342871390}
ENDPOINT = 'repos/eritpchy/video-subtitle-extractor/actions/artifacts/12624219589'


def require_hosted_ci() -> None:
    if not (os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and os.environ.get('RUNNER_OS') in ('Linux', 'macOS')):
        raise RuntimeError('Resource preparation is limited to GitHub-hosted Linux/macOS CI')
    if not os.environ.get('GH_TOKEN'):
        raise RuntimeError('The existing job must provide GH_TOKEN; no credential fallback is used')


def validate_metadata(metadata: dict) -> None:
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
    if endpoint not in (ENDPOINT, ENDPOINT + '/zip'):
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


def prepare(output: Path) -> dict:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), indent=2))


if __name__ == '__main__':
    main()
