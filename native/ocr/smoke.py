#!/usr/bin/env python3
"""Standard-library native worker smoke/parity; no Paddle/ORT Python imports."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile

from ipc_client import Worker, compare, native_evidence, raw_case

ROOT = Path(__file__).resolve().parent

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def run(artifact, timeout=120):
    artifact = Path(artifact).resolve(strict=True)
    executable = artifact / ('native-ocr.exe' if os.name == 'nt' else 'native-ocr')
    command = [str(executable), '--models', str(artifact / 'models'), '--config', str(artifact / 'config')]
    goldens = json.loads((ROOT / 'tests/goldens/smoke.json').read_text(encoding='utf-8'))
    report = {'schema_version': 1, 'worker_sha256': sha(executable), 'cases': [], 'edges': [],
              'scope': 'CPU native image worker only; not GPU, whole-GUI or clean-client certification.'}
    worker = Worker(command, timeout)
    valid = {'tier': 'small', 'path': str(ROOT / 'tests/evidence/fixtures/english.png')}
    try:
        for index, case in enumerate(goldens['cases']):
            image = ROOT / 'tests/evidence' / case['fixture']
            if sha(image) != case['fixture_sha256']:
                raise ValueError('Golden fixture hash mismatch: ' + case['fixture'])
            request = {'id': index, 'tier': case['tier'], 'operation': case['operation'], 'path': str(image)}
            if case.get('rec_model'):
                request['rec_model'] = case['rec_model']
            response = worker.request(request)
            verdict = {'passed': False, 'error': response.get('error')}
            if response['ok']:
                if case['operation'] == 'detect':
                    verdict = {'passed': response.get('dt_polys') == case['expected'], 'same_boxes': response.get('dt_polys') == case['expected']}
                else:
                    verdict = compare(response['result'], case['expected'], goldens['confidence_tolerance'])
                    verdict['text_order_correct'] = response.get('text_order') == case['text_order']
                    verdict['passed'] &= verdict['text_order_correct']
            report['cases'].append({'id': index, 'fixture': case['fixture'], 'tier': case['tier'],
                                    'rec_model': case.get('rec_model'), 'operation': case['operation'], **verdict})
        for name, request in [
            ('bad-json', b'{'), ('invalid-utf8', b'\xff'),
            ('gpu-refused', {**valid, 'provider': 'cuda'}),
            ('directml-refused', {**valid, 'provider': 'directml'}),
            ('unknown-device', {**valid, 'device': 'cuda'}),
            ('invalid-tier', {**valid, 'tier': 'invalid'}),
            ('invalid-batch', {**valid, 'batch_size': 0}),
            ('invalid-batch-type', {**valid, 'batch_size': True}),
            ('invalid-threads', {**valid, 'cpu_threads': 0}),
            ('invalid-deadline', {**valid, 'timeout_ms': 0}),
            ('traversal-model', {**valid, 'rec_model': '../escape'}),
            ('missing-model', {**valid, 'rec_model': 'nonexistent_rec'}),
            ('missing-image', {**valid, 'path': str(artifact / 'missing.png')}),
        ]:
            response = worker.request(request)
            recovery = worker.request(valid)
            report['edges'].append({'name': name, 'passed': not response['ok'] and recovery['ok'], 'response': response})
        with tempfile.TemporaryDirectory(prefix='OCR Unicode 测试 ') as temp:
            path = Path(temp) / '字幕 image.png'
            shutil.copy2(valid['path'], path)
            report['edges'].append({'name': 'unicode-spaced-image-path', 'passed': worker.request({**valid, 'path': str(path)})['ok']})
        short = worker.request({**valid, 'timeout_ms': 1})
        recovery = worker.request(valid)
        report['edges'].append({'name': 'short-deadline-recovery', 'passed': recovery['ok'], 'short_response': short})
        report['native_runtime'] = native_evidence(worker.proc.pid, executable)
        core = next((artifact / name for name in ['libonnxruntime.so.1', 'onnxruntime.dll', 'libonnxruntime.1.dylib'] if (artifact / name).is_file()), None)
        report['ort_library_sha256'] = sha(core) if core else None
    finally:
        code, stderr = worker.close()
        report['eof'] = {'returncode': code, 'passed': code == 0, 'stderr_tail': stderr[-4000:]}
    report['edges'] += [raw_case(command, 'empty-eof', b'', True),
                        raw_case(command, 'truncated-header', b'\x01'),
                        raw_case(command, 'truncated-body', struct.pack('<I', 20) + b'{}'),
                        raw_case(command, 'oversize-frame', struct.pack('<I', 65537))]
    verdicts = [c['passed'] for c in report['cases'] + report['edges']] + [report['eof']['passed']]
    if report['native_runtime'].get('available'):
        verdicts.append(report['native_runtime']['passed'])
    report['passed'] = all(verdicts)
    report['summary'] = {'parity_cases': len(report['cases']), 'edge_cases': len(report['edges']),
                         'failed': sum(not v for v in verdicts),
                         'max_confidence_delta': max(c.get('confidence_delta', 0) for c in report['cases'])}
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--artifact', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--timeout', type=float, default=120)
    a = p.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        report = run(a.artifact, a.timeout)
    except Exception as exc:
        report = {'passed': False, 'fatal_error': type(exc).__name__ + ': ' + str(exc)}
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k in ('passed', 'summary', 'fatal_error')}))
    return 0 if report['passed'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
