"""Read-only CUDA candidate checks. Never deletes libraries or certifies OCR.

A passed GPU tensor probe is not an OCR end-to-end pass. Run the bundled
application's complete GPU workload separately before approving any pruning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MODEL_SHA256 = {
    'PP-OCRv6_small_det_infer': '89240f689a4a77aad75ef55a8df0a15c8e1d4980a327d17e58f24bbadde5aeab',
    'PP-OCRv6_medium_det_infer': '0f1a7ec35da36173529c7a60238b7f7919e3831929c3f700ad90ad4896adecd5',
    'PP-OCRv6_small_rec_infer': 'f0bf53c853937a917affdd74467472167727f8ab0f0f7bded01c4a16c27e46e6',
    'PP-OCRv6_medium_rec_infer': '0b2e25e990bd072f1bf77d59d67d508bce6c4bd44af6624e0fb27d6da2cd00e8',
}


def validate_profile(*, engine='paddle', precision='float32', devices=(0,),
                     custom_models=False, training=False, profiling=False,
                     tensor_parallel=False, hpi=False):
    """Scope proposed for a future slim profile; no current runtime is changed."""
    if (engine != 'paddle' or precision != 'float32' or tuple(devices) != (0,)
            or any((custom_models, training, profiling, tensor_parallel, hpi))):
        raise ValueError('Slim CUDA candidate supports only pinned PP-OCRv6, FP32, '
                         'Paddle, gpu:0; no AMP/FP16/TRT/HPI/training/distributed/profiling')


def inspect_graph(path: Path, *, expected_sha256: str):
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise ValueError(f'Model graph changed: {path}')
    document = json.loads(data)
    operations, tensor_types = [], set()

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get('ops'), list):
                operations.extend(op['#'] for op in value['ops'])
            tag = value.get('#')
            if isinstance(tag, str) and tag.startswith('0.t_'):
                tensor_types.add(tag)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(document)
    if not operations or '0.t_f32' not in tensor_types:
        raise ValueError('Expected nonempty FP32 PIR graph')
    unsupported = ('flash', 'nccl', 'cufile', 'all_reduce', 'all_gather', 'send', 'recv')
    if any(any(token in op.lower() for token in unsupported) for op in operations):
        raise ValueError('Graph contains an operation outside the slim profile')
    if tensor_types & {'0.t_f16', '0.t_bf16', '0.t_f64'}:
        raise ValueError('Only the pinned FP32 graph profile has been audited')
    return {'sha256': digest, 'operation_count': len(operations),
            'operations': sorted(set(operations)), 'types': sorted(tensor_types)}


def gpu_tensor_probe(paddle=None):
    """Fail closed on absent CUDA; synchronize real FP32 GPU matmul.

    This intentionally cannot use HardwareAccelerator's normal CPU fallback.
    The returned status says tensor probe, not OCR verification/certification.
    """
    if paddle is None:
        import paddle
    if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() < 1:
        raise RuntimeError('Real CUDA device required; CPU fallback is not a GPU pass')
    previous = paddle.get_device()
    try:
        paddle.set_device('gpu:0')
        x = paddle.to_tensor([[1., 2.], [3., 4.]], dtype='float32',
                             place=paddle.CUDAPlace(0))
        y = paddle.matmul(x, x)
        if not y.place.is_gpu_place():
            raise RuntimeError('Tensor unexpectedly executed outside GPU')
        paddle.device.cuda.synchronize()
        if y.numpy().tolist() != [[7., 10.], [15., 22.]]:
            raise RuntimeError('CUDA matmul produced an incorrect result')
        return {'status': 'gpu-tensor-probe-passed', 'ocr_e2e_passed': False,
                'paddle': paddle.__version__, 'device': 'gpu:0',
                'device_name': paddle.device.cuda.get_device_name(0),
                'cuda': paddle.version.cuda(), 'cudnn': paddle.version.cudnn()}
    finally:
        paddle.set_device(previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, required=True, help='V6 model directory')
    parser.add_argument('--gpu-probe', action='store_true')
    args = parser.parse_args()
    validate_profile()
    result = {'status': 'static-candidate-audit-only', 'gpu_e2e_certified': False,
              'graphs': {name: inspect_graph(args.models / name / 'inference.json',
                                             expected_sha256=digest)
                         for name, digest in MODEL_SHA256.items()}}
    if args.gpu_probe:
        result['gpu_tensor_probe'] = gpu_tensor_probe()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
