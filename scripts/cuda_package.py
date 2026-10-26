"""Audit original CUDA wheel runtime bytes without claiming hardware execution.

Only the independently pinned NVJitLink static development archive may be
removed. No runtime DLL, language, model, driver or user environment is altered.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
from importlib.metadata import distributions
import json
from pathlib import Path
import sys


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def audit(root: Path, accelerator: str, *, prepare: bool = False) -> dict:
    root = root.resolve(strict=True)
    if sys.platform != 'win32' or accelerator not in ('cuda11.8', 'cuda12.6'):
        raise RuntimeError('CUDA candidate audit requires native Windows and a reviewed CUDA profile')
    packages = root / 'runtime/Lib/site-packages'
    records = {dist.metadata['Name'].lower().replace('_', '-'): dist
               for dist in distributions(path=[str(packages)])}
    if 'paddlepaddle' in records or 'paddlepaddle-gpu' not in records:
        raise RuntimeError('Exactly GPU Paddle is required; co-installed CPU Paddle is forbidden')
    if records['paddlepaddle-gpu'].version != '3.3.1':
        raise RuntimeError('CUDA package requires the reviewed Paddle GPU 3.3.1')
    runtime_files = {}
    vendors = {name: dist for name, dist in records.items()
               if name == 'paddlepaddle-gpu' or name.startswith('nvidia-')}
    for name, dist in vendors.items():
        count = 0
        for entry in dist.files or ():
            if Path(str(entry)).suffix.lower() not in ('.dll', '.pyd'):
                continue
            path = Path(dist.locate_file(entry))
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(packages)
                    or entry.hash is None or entry.hash.mode != 'sha256'):
                raise RuntimeError(f'Missing/unverifiable CUDA runtime file: {name}/{entry}')
            # RECORD may locate files through an OS ancestor alias (Windows
            # RUNNER~1 / runneradmin, macOS /var /private/var). Containment was
            # checked above against the canonical package root; use that same
            # canonical identity for hashing and the relative inventory key.
            path = path.resolve(strict=True)
            actual = digest(path)
            encoded = base64.urlsafe_b64encode(bytes.fromhex(actual)).decode().rstrip('=')
            if encoded != entry.hash.value or (entry.size is not None and path.stat().st_size != entry.size):
                raise RuntimeError(f'Changed vendor CUDA runtime file: {name}/{entry}')
            runtime_files[path.relative_to(root).as_posix()] = actual
            count += 1
        if not count:
            raise RuntimeError(f'CUDA vendor distribution contains no verified Windows runtime: {name}')
    if len(vendors) < 2:
        raise RuntimeError('Missing bundled NVIDIA CUDA runtime distributions; host CUDA must not mask them')
    import PySide6  # Initialize the same app-local Qt/VC runtime as the GUI.
    import paddle
    if not Path(paddle.__file__).resolve().is_relative_to(packages):
        raise RuntimeError('Paddle imported from outside the final package')
    if not paddle.is_compiled_with_cuda() or str(paddle.version.cuda()) != accelerator.removeprefix('cuda'):
        raise RuntimeError('Paddle CUDA compilation does not match the named package')
    architectures = getattr(paddle.version, 'compiled_cuda_archs', None)
    expected_architectures = {'cuda11.8': [61, 70, 75, 80, 86], 'cuda12.6': [75, 80, 86, 89]}[accelerator]
    if architectures != expected_architectures:
        raise RuntimeError('Paddle compiled GPU architecture metadata differs from the reviewed official wheel')
    report = {'schema': 1, 'result': 'passed', 'accelerator': accelerator,
              'paddle': paddle.__version__, 'cuda': paddle.version.cuda(),
              'cudnn': paddle.version.cudnn(),
              'compiled_cuda_archs': architectures,
              'distributions': {name: dist.version for name, dist in sorted(vendors.items())},
              'runtime_files': runtime_files, 'gpu_inference_verified': False,
              'gpu_device_probe_performed': False,
              'scope': 'Wheel runtime integrity and compiled CUDA identity; no GPU/driver/OCR certification.'}
    if prepare:
        nvjitlink = records.get('nvidia-nvjitlink-cu12')
        if nvjitlink is not None:
            try:
                from .prune_cuda_development import PINS, prune
            except ImportError:
                from prune_cuda_development import PINS, prune
            if nvjitlink.version in PINS:
                report['development_prune'] = prune(packages, apply=True)
            else:
                report['development_prune'] = {'status': 'retained', 'reason': 'Unaudited version; no files removed'}
        (root / 'runtime/cuda-package.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    else:
        original = json.loads((root / 'runtime/cuda-package.json').read_text(encoding='utf-8'))
        if (original.get('accelerator') != accelerator or original.get('runtime_files') != runtime_files
                or original.get('distributions') != report['distributions']):
            raise RuntimeError('CUDA runtime differs from its packaged vendor-byte inventory')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--accelerator', choices=('cuda11.8', 'cuda12.6'), required=True)
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.accelerator, prepare=args.prepare), indent=2))


if __name__ == '__main__':
    main()
