#!/usr/bin/env python3
"""Export three pinned original Paddle models for build-only precision replay.

This standard-library helper runs independently of model conversion/cache hits.
The macOS reference consumes the resulting artifact offline; these originals
are never part of the native inference/customer stage.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

from prepare_models import canonical, digest, extract_sources, get_archive, load_pins

HERE = Path(__file__).resolve().parent
MODEL_NAMES = (
    'cyrillic_PP-OCRv5_mobile_rec',
    'th_PP-OCRv5_mobile_rec',
    'ta_PP-OCRv5_mobile_rec',
)
SOURCE_FILES = frozenset(('inference.json', 'inference.pdiparams', 'inference.yml'))
MANIFEST = 'reference-source-manifest.json'


def _path_without_symlinks(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Reference source path cannot contain symlinks: ' + str(part))
    return path


def _pins(pins_path=None):
    # Alternate paths are for explicit internal callers/tests, not the CLI or
    # anything read from an artifact. Artifact metadata is never an authority.
    path = Path(pins_path) if pins_path is not None else HERE / 'model-source-pins.json'
    pins = load_pins(path)
    by_name = {row['name']: row for row in pins['models']}
    try:
        rows = [by_name[name] for name in MODEL_NAMES]
    except KeyError as error:
        raise ValueError('Committed pins lack a required precision reference model') from error
    for row in rows:
        if set(row['files']) != SOURCE_FILES:
            raise ValueError('Unexpected precision reference source file set')
        if type(row['archive_bytes']) is not int or row['archive_bytes'] <= 0:
            raise ValueError('Invalid pinned reference archive size')
    manifest = {
        'schema_version': 1,
        'scope': 'Build-only original Paddle reference sources; never a customer runtime artifact.',
        'source_pins_sha256': digest(path),
        'models': [{'name': row['name'], 'source_url': row['url'],
                    'archive_sha256': row['archive_sha256'],
                    'archive_bytes': row['archive_bytes'], 'files': row['files']}
                   for row in rows],
    }
    return rows, manifest


def verify(output, pins_path=None):
    """Verify exact directory contents against repository pins, without network."""
    output = _path_without_symlinks(output)
    rows, manifest = _pins(pins_path)
    if not output.is_dir():
        raise ValueError('Reference source artifact must be a directory')
    if {path.name for path in output.iterdir()} != {*MODEL_NAMES, MANIFEST}:
        raise ValueError('Unexpected reference artifact model/file set')
    for row in rows:
        directory = output / row['name']
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('Reference model must be a real directory')
        if {path.name for path in directory.iterdir()} != SOURCE_FILES:
            raise ValueError('Unexpected reference model source file set')
        for filename, expected in row['files'].items():
            source = directory / filename
            if source.is_symlink() or not source.is_file() or digest(source) != expected:
                raise ValueError('Reference source identity/hash mismatch: ' + row['name'] + '/' + filename)
    metadata = output / MANIFEST
    if metadata.is_symlink() or not metadata.is_file() or metadata.read_bytes() != canonical(manifest):
        raise ValueError('Reference source manifest differs from independently checked repository pins')
    return manifest


def _checked_archive(path, row):
    if (path.is_symlink() or not path.is_file()
            or path.stat().st_size != row['archive_bytes']
            or digest(path) != row['archive_sha256']):
        raise ValueError('Corrupt reference archive hash/size/path; refusing fallback: ' + str(path))


def prepare(output, cache, offline=False, pins_path=None, seed_cache=None):
    """Atomically export only the three required models from verified archives."""
    output = _path_without_symlinks(output)
    cache = _path_without_symlinks(cache)
    if output.exists():
        raise FileExistsError('Reference output must not already exist: ' + str(output))
    if output == cache or output in cache.parents or cache in output.parents:
        raise ValueError('Reference output and archive cache must not overlap')
    rows, manifest = _pins(pins_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    allowed_archives = {row['archive_sha256'] + '.tar' for row in rows}
    if any(path.name not in allowed_archives for path in cache.iterdir()):
        raise ValueError('Independent reference cache may contain only the three pinned archives')
    seed = _path_without_symlinks(seed_cache) if seed_cache is not None else None
    if seed is not None and seed == cache:
        raise ValueError('Seed and independent reference archive cache must be separate')
    if seed is not None and seed.exists() and not seed.is_dir():
        raise ValueError('Seed archive cache must be a directory when present')
    # Validate existing bytes before downloading/copying anything. A good cache
    # hit must not hide a corrupt seed (nor may a seed replace a corrupt hit).
    for row in rows:
        name = row['archive_sha256'] + '.tar'
        for path in (cache / name, *((seed / name,) if seed is not None else ())):
            if path.exists() or path.is_symlink():
                _checked_archive(path, row)
    with tempfile.TemporaryDirectory(prefix='.precision-sources-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'stage'
        stage.mkdir()
        for row in rows:
            cached = cache / (row['archive_sha256'] + '.tar')
            # get_archive rejects ordinary corrupt hits. Also reject dangling
            # symlinks, whose exists() is false, before a network attempt.
            if cached.is_symlink():
                raise ValueError('Reference archive cache entry cannot be a symlink')
            if not cached.exists() and seed is not None and (seed / cached.name).is_file():
                with tempfile.NamedTemporaryFile(prefix='.reference-seed-', dir=cache, delete=False) as file:
                    staged_archive = Path(file.name)
                try:
                    shutil.copyfile(seed / cached.name, staged_archive)
                    _checked_archive(staged_archive, row)
                    os.replace(staged_archive, cached)
                finally:
                    staged_archive.unlink(missing_ok=True)
            archive = get_archive(row, cache, offline)
            _checked_archive(archive, row)
            extract_sources(archive, row, stage / row['name'])
        (stage / MANIFEST).write_bytes(canonical(manifest))
        verify(stage, pins_path)
        if output.exists() or output.is_symlink():
            raise FileExistsError('Refusing to replace reference output created during export')
        os.replace(stage, output)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--output', type=Path)
    action.add_argument('--verify', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--seed-cache', type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if args.verify is not None:
        if args.cache is not None or args.seed_cache is not None or args.offline:
            parser.error('--verify is always offline and accepts no cache or offline options')
        manifest = verify(args.verify)
    else:
        if args.cache is None:
            parser.error('--output requires --cache')
        manifest = prepare(args.output, args.cache, args.offline, seed_cache=args.seed_cache)
    print(canonical({'verified_models': [row['name'] for row in manifest['models']],
                     'source_pins_sha256': manifest['source_pins_sha256'],
                     'customer_runtime': False}).decode(), end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
