"""Dependency-free verification of fixed ONNX/config bytes, not worker builds."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value or str(path) != value:
        raise ValueError('Unsafe relative path: ' + value)
    return path


def verify_models(root, *, pins_path=None):
    root = Path(root).resolve(strict=True)
    rows = json.loads((root / 'model-manifest.json').read_text(encoding='utf-8'))
    pins = json.loads((Path(pins_path) if pins_path is not None else ROOT / 'model-source-pins.json').read_text(encoding='utf-8'))
    expected = {m['name'] + '_infer': m for m in pins['models']}
    names = [r['name'] for r in rows]
    if len(rows) != 15 or len(set(names)) != 15:
        raise ValueError('Exactly 15 distinct pinned model entries are required')
    if set(names) != set(expected):
        raise ValueError('Model identities differ from reviewed source pins')
    for row in rows:
        name = row['name']
        if row['onnx_sha256'] != expected[name]['onnx_sha256'] or row['onnx_bytes'] != expected[name]['onnx_bytes']:
            raise ValueError('Model bytes differ from reviewed deterministic conversion: ' + name)
        source_name = expected[name]['name']
        config = ('config/' + source_name.split('_')[1] + '.json'
                  if source_name.startswith('PP-OCRv6_') else 'config/' + source_name + '.json')
        if row['config_file'] != config:
            raise ValueError('Config identity differs from reviewed model binding: ' + name)
        if pins['config_sha256'].get(row['config_file']) != row['config_sha256']:
            raise ValueError('Config differs from reviewed source pins: ' + name)
        if not isinstance(name, str) or len(relative(name).parts) != 1 or not name.endswith('_infer'):
            raise ValueError('Invalid model name')
        for rel, digest in [('models/' + name + '.onnx', row['onnx_sha256']),
                            (row['config_file'], row['config_sha256'])]:
            relative(rel)
            p = root / rel
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(root):
                raise ValueError('Invalid model asset path: ' + rel)
            if sha256(p) != digest:
                raise ValueError('Model/config SHA256 mismatch: ' + rel)
        if (root / 'models' / (name + '.onnx')).stat().st_size != row['onnx_bytes']:
            raise ValueError('Model byte count mismatch: ' + name)
    return rows
