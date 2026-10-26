#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exact accepted-306 CUDA byte-volume restoration, then original-launcher audit."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
RESTORER = HERE.parent / 'windows-restorer'
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(RESTORER)); sys.path.insert(0, str(HERE))
import accepted_baseline as baseline
import build as builder
import fixed_index
import generate_metadata
import source_bundle
from scripts import release_volume_transport as transport
from scripts.accept_release_archive import read_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-directory', required=True, type=Path)
    parser.add_argument('--pins', required=True, type=Path)
    parser.add_argument('--profile', required=True, choices=('paddle-cuda11.8', 'paddle-cuda12.6'))
    parser.add_argument('--baseline-source', required=True, type=Path)
    parser.add_argument('--runner', choices=('windows-2022', 'windows-2025'), required=True)
    args = parser.parse_args(argv)
    if os.name != 'nt' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('This real baseline runs only on authorized native Windows Servers')
    work = args.work_directory.absolute(); work.mkdir(parents=True, exist_ok=False)
    pins = baseline.load_pins(args.pins); row = next(r for r in pins['profiles'] if r['profile'] == args.profile)
    report = {'schema': 1, 'scope': 'accepted-306-baseline-native-restore-not-final-release', 'result': 'failed',
              'runner': args.runner, 'baseline_commit': pins['commit'], 'restorer_commit': source_bundle.git_identity(ROOT),
              'profile': args.profile, 'archive_sha256': row['archive_sha256'], 'pins_sha256': baseline.sha(args.pins),
              'gpu_inference_verified': False, 'client_windows_10_11_verified': False, 'release_distribution_created': False,
              'physical_disk_exhaustion_tested': False, 'disk_checks': []}
    original_files = None; expected = None; destination = None; volume_manifest = None; failure = None
    try:
        report['disk_checks'].append(baseline.require_free(work, row['artifact_bytes'] + row['archive_bytes'] + baseline.RESERVE, 'before-download'))
        container = work / 'original-artifact.zip'
        report['artifact'] = baseline.acquire_artifact(pins, row, container, os.environ.get('GITHUB_TOKEN', ''))
        os.environ.pop('GITHUB_TOKEN', None)  # No download credential in compiler/app child environments.
        original = work / 'original'
        original_files = baseline.extract_original(container, original, row)
        report['original_files'] = original_files
        archive = original / row['archive_name']
        identity = read_manifest(archive)
        if (identity['commit'] != pins['commit'] or identity['build_profile'] != row['profile']
                or identity['platform'] != 'windows' or identity['arch'] != 'x64'):
            raise RuntimeError('Original accepted package identity mismatch')
        # Exact-container verification and successful bounded unwrapping precede
        # removing only this invocation's own transfer container, never user data.
        if baseline.sha(container) != row['artifact_sha256']: raise RuntimeError('Original container changed')
        container.unlink()
        # Plan native metadata overhead without decompressing or duplicating the
        # original payload. The full transport verifier below independently binds
        # the inventory and SHA to the original accepted ZIP and receipt.
        # Upper-bound before splitting; exact inventory replaces it immediately
        # after strict verification. Use actual central-directory names only.
        import zipfile
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            expanded = sum(info.file_size for info in entries)
            names = {info.filename.rstrip('/') for info in entries}
            for info in entries:
                parts = info.filename.rstrip('/').split('/')
                names.update('/'.join(parts[:index]) for index in range(1, len(parts)))
        if expanded != row['expanded_bytes']: raise RuntimeError('Actual expanded size differs from fixed 306 evidence')
        report['budget'] = baseline.disk_budget(row, len(names))
        report['disk_checks'].append(baseline.require_free(work, report['budget']['after_original_download_extra_bytes'], 'before-volumes-and-native-restore'))
        volume_manifest = transport.create_transport(archive, work / 'volumes')
        document = transport.verify_transport(volume_manifest, include_entries=True)
        report['artifact']['original_acceptance_not_yet_verified'] = False
        report['original_acceptance_validation'] = 'passed-release-guard-bound-to-original-artifact-and-ZIP'
        expected = baseline.inventory(document)
        if len(expected) != len(names) or document['archive']['uncompressed_bytes'] != expanded:
            raise RuntimeError('Verified native inventory differs from preflight budget')
        report['transport'] = {'manifest_sha256': baseline.sha(volume_manifest), 'archive': document['archive'], 'parts': document['parts'],
                               'receipt': document['receipt'], 'checksum': document['checksum'], 'part_bytes': document['part_bytes']}
        document['fixed_zip_index'] = fixed_index.from_verified_transport(volume_manifest, document)
        build = work / 'native-build'; build.mkdir()
        header = generate_metadata.render_verified(document).encode('ascii')
        (build / 'bundle_metadata.hpp').write_bytes(header)
        snapshot = source_bundle.snapshot_sources(ROOT)
        companion = build / 'fixture-source'; companion.mkdir()
        for name, data in snapshot.items():
            path = companion / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        (companion / 'bundle_metadata.hpp').write_bytes(header)
        # Compile sources remain the unchanged restorer closure. Preserve the
        # independent experiment orchestrator separately for exact CI replay.
        orchestration = build / 'experiment-source'; orchestration.mkdir()
        harness_sources = sorted(HERE.rglob('*.py')) + [HERE / 'accepted-baseline-306.json', HERE / 'README.md',
            ROOT / 'scripts/run_windows_restorer_accepted_ci.py', ROOT / '.github/workflows/windows-restorer-accepted.yml']
        report['experiment_source_sha256'] = {}
        for path in harness_sources:
            name = path.relative_to(ROOT).as_posix(); data = path.read_bytes()
            target = orchestration / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            report['experiment_source_sha256'][name] = hashlib.sha256(data).hexdigest()
        report['build'] = builder.compile_fixed(RESTORER, build, build)
        source_bundle.assert_sources_unchanged(ROOT, snapshot)
        if (build / 'bundle_metadata.hpp').read_bytes() != header: raise RuntimeError('Fixed header changed during compilation')
        report['build'].update(embedded_header_sha256=hashlib.sha256(header).hexdigest(),
            source_sha256={name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()},
            distribution_scope='SERVER-DIAGNOSTIC-ONLY-NOT-FOR-DISTRIBUTION')
        (build / 'accepted-build-record.json').write_text(json.dumps(report['build'], indent=2) + '\n', encoding='utf-8')
        destination = work / 'restored Unicode 中文 &!()'; destination.mkdir()
        command = [str(build / 'restore.exe'), '--source', str(work / 'volumes'), '--destination-parent', str(destination), '--non-interactive']
        report['restore_command'] = command
        with (work / 'native-restore-stdout.log').open('wb') as out, (work / 'native-restore-stderr.log').open('wb') as err:
            result = subprocess.run(command, stdout=out, stderr=err, timeout=1800)
        report['restore_returncode'] = result.returncode; result.check_returncode()
        report['restored_before_launcher'] = baseline.verify_restored_tree(destination, expected)
        root = destination / document['archive']['root']
        command = [sys.executable, '-B', '-X', 'utf8', str(HERE / 'restored_launcher_audit.py'),
                   '--baseline-source', str(args.baseline_source.absolute()), '--baseline-commit', pins['commit'],
                   '--root', str(root), '--output', str(work / 'launcher-evidence'), '--runner', args.runner]
        report['audit_command'] = command
        with (work / 'launcher-audit.log').open('wb') as output:
            audited = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=11000)
        audited.check_returncode()
        audit = json.loads((work / 'launcher-evidence/restored-launcher-report.json').read_bytes())
        if audit.get('result') != 'passed' or len(audit.get('scenarios', [])) != 2:
            raise RuntimeError('Original restored launcher acceptance is incomplete')
        report['launcher'] = audit
    except Exception as error:
        failure = error
        report['error'] = f'{type(error).__name__}: {error}'
        if hasattr(error, 'disk_record'): report['disk_checks'].append(error.disk_record)
    finally:
        # Failure evidence is also important: do not skip the after-tree/input
        # checks merely because an application workload failed, and never mask
        # that primary error with a secondary audit error.
        post_errors = []
        if destination is not None and expected is not None:
            try: report['restored_after_launcher'] = baseline.verify_restored_tree(destination, expected)
            except Exception as error: post_errors.append({'check': 'restored-tree-after', 'error': str(error)})
        if original_files is not None:
            try:
                report['originals_after'] = baseline.verify_originals(work / 'original', original_files)
                report['originals_unchanged_verified'] = True
            except Exception as error: post_errors.append({'check': 'original-ZIP-and-sidecars-after', 'error': str(error)})
        if volume_manifest is not None:
            try:
                transport.verify_transport(volume_manifest)
                report['volumes_unchanged_verified'] = True
            except Exception as error: post_errors.append({'check': 'volumes-after', 'error': str(error)})
        report['post_run_errors'] = post_errors
        if failure is None and post_errors: failure = RuntimeError('Post-run integrity checks failed')
        if failure is None:
            report['result'] = 'passed'
            try: baseline.validate_run_report(report, pins, args.profile, args.runner)
            except Exception as error: failure = error
        if failure is not None:
            report['result'] = 'failed'
            report.setdefault('error', f'{type(failure).__name__}: {failure}')
        (work / 'accepted-restorer-report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if failure is not None: raise failure
    return 0


if __name__ == '__main__': raise SystemExit(main())
