"""Single source of truth for honest, self-contained release candidate identities.

CUDA 10.2 remains a historical profile, not a renamed CPU package.
Its Paddle 2.5.2/Python 3.11 stack cannot execute the current PaddleOCR 3.7/PIR
models. Its immutable 2.2.0 download is retained; the approved current matrix
uses explicit CUDA 11.8/12.6 names, never pretending to restore CUDA 10.2.
"""
from __future__ import annotations

from pathlib import Path

PROFILES = {
    'paddle-cpu': {'backend': 'paddle', 'accelerator': 'cpu', 'suffix': 'cpu',
                   'requirements': 'requirements-inference.txt'},
    'native-cpu': {'backend': 'native', 'accelerator': 'cpu', 'suffix': 'native-cpu',
                   'requirements': 'requirements-native-inference.txt'},
    'paddle-cuda11.8': {'backend': 'paddle', 'accelerator': 'cuda11.8', 'suffix': 'cuda11.8',
                       'requirements': 'requirements-inference.txt',
                       'gpu_index': 'https://www.paddlepaddle.org.cn/packages/stable/cu118/'},
    'paddle-cuda12.6': {'backend': 'paddle', 'accelerator': 'cuda12.6', 'suffix': 'cuda12.6',
                       'requirements': 'requirements-inference.txt',
                       'gpu_index': 'https://www.paddlepaddle.org.cn/packages/stable/cu126/'},
    'directml': {'backend': 'directml', 'accelerator': 'directml', 'suffix': 'directml',
                'requirements': 'requirements-directml-inference.txt'},
}
CANDIDATE_TARGETS = {
    **{(platform, arch, 'native-cpu'): 'native-cpu'
       for platform, arch in (('linux', 'x64'), ('windows', 'x64'), ('macos', 'arm64'))},
    **{('windows', 'x64', name): PROFILES[name]['suffix']
       for name in ('paddle-cpu', 'paddle-cuda11.8', 'paddle-cuda12.6', 'directml')},
}
LEGACY_PROFILE_GAPS = {
    'paddle-cuda10.2': 'Historical CUDA 10.2 requires Paddle 2.5.2.post102 and Python 3.11; '
                      'the current PaddleOCR 3.7 / PP-OCRv6 PIR path requires Paddle 3.x. '
                      'No CPU relabeling, silent OCR downgrade or GPU-certification claim is permitted.',
}
# The user approved retaining the immutable old CUDA 10.2 download rather than
# downgrading current models or relabeling another runtime. This is not approval
# to publish, change a version, or modify any existing tag/release asset.
BLOCKED_PROFILES = {
    'native-coreml': 'The required production Mac package must support real CoreML/GPU acceleration '
                     'and explicit CPU in one environment. CPU-only candidate success is a baseline, '
                     'not completion; production provider integration and actual Mac GPU evidence '
                     'are still pending. Never relabel a CPU-only package.',
}


def profile_identity(backend: str, accelerator: str = 'cpu') -> tuple[str, dict]:
    if backend == 'directml' and accelerator == 'cpu':
        accelerator = 'directml'  # Package identity; explicit --cpu inference remains available.
    matches = [(name, value) for name, value in PROFILES.items()
               if value['backend'] == backend and value['accelerator'] == accelerator]
    if len(matches) != 1:
        raise ValueError(f'Unsupported release backend/accelerator: {backend}/{accelerator}')
    return matches[0]


def archive_name(version: str, platform: str, arch: str, build_profile: str) -> str:
    profile = PROFILES[build_profile]
    if profile['accelerator'] != 'cpu' and (platform, arch) != ('windows', 'x64'):
        raise ValueError('Restored GPU candidates are Windows x64 only; no unverified cross-platform claims')
    return f"vse-{version}-{platform}-{arch}-{profile['suffix']}.zip"


def profile_requirements(root: Path, build_profile: str) -> list[str]:
    profile = PROFILES[build_profile]
    lines = [line.strip() for line in (root / profile['requirements']).read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if 'gpu_index' in profile:
        if lines.count('paddlepaddle==3.3.1') != 1:
            raise RuntimeError('CUDA profile requires exactly the reviewed Paddle 3.3.1 CPU root to replace')
        lines[lines.index('paddlepaddle==3.3.1')] = 'paddlepaddle-gpu==3.3.1'
    if build_profile == 'paddle-cuda12.6':
        # Paddle's pinned cuFFT/cuSolver/cuSparse wheels declare NVJitLink but
        # omit its version. Keep the independently audited CUDA 12.6 release;
        # do not accidentally pull a newer >1 GB static developer archive.
        lines.append('nvidia-nvjitlink-cu12==12.6.85')
    return lines
