#!/usr/bin/env python3
"""Verify untrusted converted-model caches using committed pins, stdlib only.

Run after both cache restore and conversion, before saving/uploading any bytes.
No cache-supplied pin, manifest digest, or success marker is an authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from build import verify_models
from prepare_models import load_pins

ROOT = Path(__file__).resolve().parent


def verify(root):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Model cache must be a regular directory')
    pins = load_pins(ROOT / 'model-source-pins.json')
    expected_files = {'model-manifest.json', 'model-source-pins.json', 'unavailable-models.json',
                      *pins['config_sha256'],
                      *('models/' + m['name'] + '_infer.onnx' for m in pins['models'])}
    actual_files = set()
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Invalid model cache asset: ' + str(path))
        if path.is_file():
            actual_files.add(path.relative_to(root).as_posix())
        elif path.relative_to(root).as_posix() not in {'models', 'config'}:
            raise ValueError('Unexpected model cache directory: ' + str(path))
    if actual_files != expected_files:
        raise ValueError('Model cache file set differs from reviewed conversion output')
    if json.loads((root / 'model-source-pins.json').read_text(encoding='utf-8')) != pins:
        raise ValueError('Cached source pins differ from committed source pins')
    if json.loads((root / 'unavailable-models.json').read_text(encoding='utf-8')) != []:
        raise ValueError('All 15 pinned models must be available')

    # Reuses the native build's independent identity, size and SHA256 checks.
    # That verifier reads its own committed pins, never the cache's copy.
    rows = verify_models(root)
    expected = {m['name'] + '_infer': m for m in pins['models']}
    for row in rows:
        source = expected[row['name']]
        name = source['name']
        config = row['config_file']  # Model/config binding is checked by verify_models.
        manifest = {
            'name': name + '_infer', 'rec_model': name if name.endswith('_rec') else None,
            'onnx_sha256': source['onnx_sha256'], 'onnx_bytes': source['onnx_bytes'],
            'source_url': source['url'], 'source_archive_sha256': source['archive_sha256'],
            'source': {k: {'sha256': v} for k, v in source['files'].items()},
            'languages': source.get('languages', []), 'converter': pins['converter'],
            'config_file': config, 'config_sha256': pins['config_sha256'][config],
        }
        if row != manifest:
            raise ValueError('Model/config identity or provenance differs from committed pins: ' + name)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('models', type=Path)
    args = parser.parse_args()
    rows = verify(args.models)
    print(f'Independently verified {len(rows)} pinned models and all config/provenance files')


if __name__ == '__main__':
    main()
