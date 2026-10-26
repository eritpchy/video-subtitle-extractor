#!/usr/bin/env python3
"""Pinned CI seed bootstrap from existing accepted bytes, never model origins.

This artifact is a temporary bootstrap, not durable storage. If it has expired
and the independently verified cache is unavailable, fail closed. Preserve the
seed outside ephemeral CI before updating this reviewed source contract.
"""
from __future__ import annotations

import argparse
from pathlib import Path

try:
    from .paddle_model_seed import seed_models, verify_seed
    from .paddle_package_models import PINNED_SOURCE_SHA256
except ImportError:
    from paddle_model_seed import seed_models, verify_seed
    from paddle_package_models import PINNED_SOURCE_SHA256

SOURCE_RUN = '40259119763'
SOURCE_ARTIFACT = '12624088363'  # Recorded origin ID; v4 retrieval binds run/name + both fixed hashes.
SOURCE_ARTIFACT_NAME = 'release-candidate-windows-x64-paddle-cpu'
SOURCE_SHA = '59749ccc02e369f374c69846b2bde6279084577d'
ARCHIVE = 'vse-2.3.0-windows-x64-cpu.zip'
ARCHIVE_BYTES = 583979746
ARCHIVE_SHA256 = '6318016c3e0e1452e2aa88d1ca1757b0934a02f677d749d69330a91836bc9fc8'
RECEIPT_SHA256 = 'dbca5f75e163bba0620741c1cfee96a65b476279c22aeb7fbf0c4337b51c9f41'
SOURCE_ARTIFACT_EXPIRES = '2026-10-27T02:26:40Z'
ANCHORS = dict(source_sha=SOURCE_SHA, artifact_run=SOURCE_RUN,
               archive_sha256=ARCHIVE_SHA256, receipt_sha256=RECEIPT_SHA256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--bootstrap-directory', type=Path)
    action.add_argument('--verify-seed', type=Path)
    action.add_argument('--github-outputs', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.github_outputs is not None:
        if args.output is not None:
            parser.error('--github-outputs does not take --output')
        outputs = {'source_run': SOURCE_RUN, 'source_artifact_name': SOURCE_ARTIFACT_NAME,
                   'cache_key': 'paddle-original-seed-v1-' + PINNED_SOURCE_SHA256 + '-' + ARCHIVE_SHA256}
        with args.github_outputs.open('a', encoding='utf-8', newline='\n') as stream:
            for name, value in outputs.items():
                stream.write(name + '=' + value + '\n')
        return 0
    if args.verify_seed is not None:
        if args.output is not None:
            parser.error('--verify-seed does not take --output')
        report = verify_seed(args.verify_seed, **ANCHORS)
    else:
        if args.output is None:
            parser.error('--bootstrap-directory requires a new --output')
        archive = args.bootstrap_directory / ARCHIVE
        if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != ARCHIVE_BYTES:
            raise RuntimeError('Pinned accepted CPU bootstrap ZIP absent or wrong size; no model-origin fallback is permitted')
        report = seed_models(archive, archive.with_suffix('.zip.sha256'),
                             archive.with_suffix('.zip.acceptance.json'), args.output, **ANCHORS)
    print('Verified offline original-model seed: 15 models; original source ' + report['source_sha'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
