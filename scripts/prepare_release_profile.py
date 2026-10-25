"""Resolve original vendor wheels on the build host; never install on clients."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from .release_profiles import PROFILES, profile_requirements
    from .runtime_dependencies import runtime_wheels
except ImportError:
    from release_profiles import PROFILES, profile_requirements
    from runtime_dependencies import runtime_wheels

ROOT = Path(__file__).resolve().parents[1]


def prepare(profile_name: str, output: Path) -> None:
    profile = PROFILES[profile_name]
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('Use a new/empty wheelhouse; mixed CPU/GPU wheels are forbidden')
    output.mkdir(parents=True, exist_ok=True)
    requirements = profile_requirements(ROOT, profile_name)
    with tempfile.TemporaryDirectory(prefix='vse-profile-wheels-') as temporary:
        temporary = Path(temporary)
        resolver_roots = list(requirements)
        vendor_wheels = []
        if 'gpu_index' in profile:
            if sys.platform != 'win32':
                raise RuntimeError('CUDA package resolution must run on the native Windows Server CI host')
            # Select Paddle ONLY from its official versioned CUDA index. Resolve
            # ordinary transitive dependencies from the normal trusted PyPI index;
            # never combine CPU/GPU Paddle or depend on global CUDA installations.
            subprocess.run([sys.executable, '-m', 'pip', 'download', '--no-cache-dir', '--only-binary=:all:', '--no-deps',
                            '--index-url', profile['gpu_index'], '--dest', str(temporary),
                            'paddlepaddle-gpu==3.3.1'], check=True)
            vendor_wheels = sorted(temporary.glob('paddlepaddle_gpu-3.3.1-*.whl'))
            if len(vendor_wheels) != 1:
                raise RuntimeError('Expected exactly one original Paddle GPU 3.3.1 wheel')
            resolver_roots.remove('paddlepaddle-gpu==3.3.1')
        requirements_file = temporary / 'requirements.txt'
        requirements_file.write_text('\n'.join(resolver_roots) + '\n', encoding='utf-8')
        resolved = temporary / 'resolved'
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-cache-dir', '-r', str(requirements_file),
                        *map(str, vendor_wheels), '--wheel-dir', str(resolved)], check=True)
        selected, roots = runtime_wheels(resolved, requirements)
        if profile_name == 'paddle-cuda12.6':
            audited = [wheel for wheel in selected if wheel.name.startswith('nvidia_nvjitlink_cu12-12.6.85-')]
            if len(audited) != 1:
                raise RuntimeError('Expected the independently audited NVJitLink 12.6.85 Windows wheel')
            with audited[0].open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            if checksum != 'e61120e52ed675747825cdd16febc6a0730537451d867ee58bee3853b1b13d1c':
                raise RuntimeError('NVJitLink wheel differs from the official audited PyPI SHA256')
        import shutil
        for wheel in selected:
            shutil.move(str(wheel), output / wheel.name)
        (output / 'profile.json').write_text(json.dumps({'build_profile': profile_name,
            'runtime_requirements': roots, 'vendor_index': profile.get('gpu_index'),
            'gpu_inference_verified': False}, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=tuple(PROFILES), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.profile, args.output)


if __name__ == '__main__':
    main()
