"""Remove only a pinned Windows NVJitLink static development archive.

The CUDA DLL, import library, licenses and distribution metadata remain intact.
This is not GPU inference certification and never removes a runtime DLL/SO.
"""
from __future__ import annotations

import argparse
import base64
from email.parser import Parser
import hashlib
import json
from pathlib import Path
import os

STATIC = 'nvidia/nvjitlink/lib/x64/nvJitLink_static.lib'
DLL = 'nvidia/nvjitlink/bin/nvJitLink_120_0.dll'
# Official wheel RECORD hashes. Whole-wheel PyPI SHA256 provenance is captured
# separately in docs/audits/windows-nvjitlink-development-2026-10-25.json.
PINS = {
    '12.6.85': {STATIC: ('P-jHA29E_PMvlLW3cU3TYInNCejHDSl0hio7HQUs0xc', 453528212),
                DLL: ('i9_jmttyv0_X7WZdwkLfHkjaAVtkFlEYPTXpp9t2q-g', 39236608)},
    '12.8.93': {STATIC: ('39YAYtqUuXYM0MIChMTqVrNqDx00rEv4veyRkOcZZDo', 1001900552),
                DLL: ('lZ08tEUn7IhNuNwgdyUgtYTb59Yi0RsFMOYyZBcHiz4', 77860352)},
}


def checked_file(root, relative):
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError('Path escaped runtime root')
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'Symlink rejected: {parent}')
    if not path.is_file():
        raise ValueError(f'Required file missing: {path}')
    return path


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).decode().rstrip('=')


def write_report(path, report):
    # Never follow existing audit-file symlinks; replacement is atomic.
    if path.is_symlink():
        raise ValueError('Audit symlink rejected')
    temporary = path.with_name(path.name + '.tmp')
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('Previous audit temporary file exists; inspect before retrying')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def prune(root: Path, *, apply=False):
    if root.is_symlink():
        raise ValueError('Runtime root symlink rejected')
    root = root.resolve(strict=True)
    metadata = list(root.glob('nvidia_nvjitlink_cu12-*.dist-info/METADATA'))
    if len(metadata) != 1:
        raise ValueError('Exactly one NVJitLink distribution is required')
    metadata_file = checked_file(root, metadata[0].relative_to(root))
    fields = Parser().parsestr(metadata_file.read_text())
    version = fields['Version']
    if fields['Name'] != 'nvidia-nvjitlink-cu12' or version not in PINS:
        raise ValueError('Unsupported NVJitLink identity/version; no files removed')
    info = metadata_file.parent
    wheel = checked_file(root, (info / 'WHEEL').relative_to(root)).read_text()
    if 'win_amd64' not in wheel:
        raise ValueError('Only audited Windows x64 wheel payloads are supported')
    checked_file(root, (info / 'License.txt').relative_to(root))
    checked_file(root, (info / 'RECORD').relative_to(root))
    checked_file(root, 'nvidia/nvjitlink/lib/x64/nvJitLink.lib')
    verified = []
    for relative, (digest, size) in PINS[version].items():
        path = checked_file(root, relative)
        if path.stat().st_size != size or digest_file(path) != digest:
            raise ValueError(f'Unrecognized or corrupted CUDA file: {relative}')
        verified.append({'path': relative, 'bytes': size, 'sha256_base64url': digest})
    static = checked_file(root, STATIC)
    with static.open('rb') as stream:
        if stream.read(8) != b'!<arch>\n':
            raise ValueError('Expected a static archive, never a runtime DLL')
    report = {'status': 'dry-run', 'distribution': 'nvidia-nvjitlink-cu12',
              'version': version, 'gpu_inference_verified': False,
              'removed': [item for item in verified if item['path'] == STATIC],
              'retained': [item for item in verified if item['path'] == DLL],
              'saved_bytes': PINS[version][STATIC][1]}
    if apply:
        audit = root / 'cuda-development-prune.json'
        report['status'] = 'prepared'
        write_report(audit, report)
        static.unlink()
        report['status'] = 'applied'
        write_report(audit, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site_packages', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prune(args.site_packages, apply=args.apply), indent=2))
