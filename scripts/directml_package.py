"""Stage only the previously validated 15 FP32 ONNX graphs and JSON configs."""
from pathlib import Path
import shutil

from backend.tools.directml_ocr import validate_model_assets, _sha, ORT_VERSION


def stage_directml_models(source: Path, destination: Path) -> dict:
    verified = validate_model_assets(source)
    destination.mkdir(parents=True, exist_ok=False)
    files = {}
    for relative, path in verified['files'].items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        digest = _sha(target)
        if digest != verified['hashes'][relative]:
            raise ValueError(f'DirectML model changed while staging: {relative}')
        files[relative] = digest
    licenses = Path(__file__).resolve().parents[1] / 'docs/licenses/directml-1.15.2'
    import json
    source = json.loads((licenses / 'source.json').read_text(encoding='utf-8'))
    for name, digest in source['files'].items():
        path = licenses / name
        if _sha(path) != digest:
            raise ValueError('Original DirectML 1.15.2 notice checksum mismatch: ' + name)
    for name in ('LICENSE.txt', 'ThirdPartyNotices.txt', 'source.json'):
        target = destination / 'licenses/DirectML-1.15.2' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(licenses / name, target)
        files['licenses/DirectML-1.15.2/' + name] = _sha(target)
    return {'profile': 'directml', 'model_count': 15, 'onnxruntime': ORT_VERSION,
            'precision': 'FP32', 'conversion_on_client': False, 'files': files,
            'gpu_hardware_verified': False,
            'note': 'CPU parity and DirectML GPU execution are distinct validation gates.'}


def audit_directml_runtime(site_packages: Path) -> dict:
    """Reject a newer/replaced DirectML SDK disguised behind the original version."""
    import importlib.metadata
    distributions = {dist.metadata['Name'].lower().replace('_', '-'): dist
                     for dist in importlib.metadata.distributions(path=[str(site_packages)])}
    runtime = distributions.get('onnxruntime-directml')
    if runtime is None or runtime.version != ORT_VERSION:
        raise ValueError('DirectML package must contain original onnxruntime-directml 1.20.1')
    forbidden = {'onnxruntime', 'onnxruntime-gpu', 'paddlepaddle', 'paddlepaddle-gpu', 'paddleocr', 'paddlex', 'paddle2onnx'} & distributions.keys()
    if forbidden:
        raise ValueError(f'DirectML package has conflicting inference distributions: {sorted(forbidden)}')
    binaries = {
        'DirectML.dll': '7e64aab30990e4b772751719a42290304d7502c99e75ae8164529d75b35c42f1',
        'onnxruntime.dll': '8b08b14301eac0d144e241b91bfd342048d863060b2048a7280bac482f4e7010',
        'onnxruntime_providers_shared.dll': '08735f441fb17d0d0c8523242d4d7234f381d5a456a84fd0ce843014bcd2165b',
        'onnxruntime_pybind11_state.pyd': '533b7120d66fb765a7b563658556528d3dd9cb4b77e20fffdab3e9e475a3bd8c',
    }
    for name, expected in binaries.items():
        dll = site_packages / 'onnxruntime/capi' / name
        if not dll.is_file() or dll.is_symlink() or _sha(dll) != expected:
            raise ValueError(f'{name} is not the original reviewed runtime from the 1.20.1 cp312 x64 wheel')
    return {'onnxruntime_directml': ORT_VERSION, 'directml': '1.15.2', 'native_binaries': binaries,
            'license_source': 'directml-models/licenses/DirectML-1.15.2/source.json',
            'gpu_hardware_verified': False}
