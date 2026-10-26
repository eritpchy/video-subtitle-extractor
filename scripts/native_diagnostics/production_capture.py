"""Explicit fixed-fixture capture in the actual packaged worker, not a proxy.

Run as python -m scripts.native_diagnostics.production_capture. This evidence
command is never packaged or called by the GUI. It does not certify GPU dispatch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct
import subprocess

from . import benchmark as diag


def environment():
    env = diag.diagnostic_environment(True)
    # Only the existing worker's own loader path may resolve its pinned runtime.
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH',
                'DYLD_FRAMEWORK_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'DYLD_FALLBACK_FRAMEWORK_PATH'):
        env.pop(key, None)
    env['VSE_OCR_PARENT_PID'] = str(os.getpid())
    return env


def inputs(args):
    root = args.native_root.resolve(strict=True)
    worker = root / 'native-ocr'
    worker_identity = diag.pinned_file(worker, args.worker_sha256)
    library = root / ('libonnxruntime.1.dylib' if diag.target_for_host() == 'macos-arm64'
                      else 'libonnxruntime.so.1')
    runtime = diag.verify_runtime(library)
    models, configs = diag.verify_models(root)
    binding = {'scope': 'native-build-artifact', 'final_bundle_verified': False}
    manifest_path = args.bundle_manifest
    if manifest_path:
        manifest_path = manifest_path.resolve(strict=True)
        if (manifest_path.parent / 'native-ocr').resolve(strict=True) != root:
            raise ValueError('Bundle manifest does not own the requested worker directory')
        manifest = diag.read_json(manifest_path)
        expected = {'ocr_backend': 'native', 'build_profile': 'native-coreml',
                    'platform': 'macos', 'arch': 'arm64'}
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise ValueError('Only the exact final native-coreml Mac bundle can receive final identity')
        native_files = {name.removeprefix('native-ocr/'): sha for name, sha in manifest['files'].items()
                        if name.startswith('native-ocr/')}
        required = {'native-ocr', library.name}
        required.update('models/' + name for name in models)
        required.update(configs)
        if not required.issubset(native_files):
            raise ValueError('Final bundle manifest lacks the exact worker/runtime/model inputs')
        for name, sha in native_files.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or (root / relative).resolve().is_relative_to(root) is False:
                raise ValueError('Unsafe native file in final bundle manifest')
            diag.pinned_file(root / relative, sha)
        binding = {'scope': 'final-bundle-worker', 'final_bundle_verified': True,
                   'manifest_sha256': diag.digest(manifest_path),
                   'commit': manifest.get('commit'), 'version': manifest.get('version'),
                   'build_profile': manifest['build_profile'],
                   'verification_scope': 'Manifest-bound native payload only, not complete GUI/client acceptance'}
    return root, worker, {'worker': worker_identity, 'runtime': runtime,
                          'models': models, 'configs': configs, 'binding': binding}


def capabilities(worker):
    result = subprocess.run([str(worker), '--diagnostic-metal-capabilities'], env=environment(),
                            capture_output=True, timeout=60, check=True)
    if len(result.stdout) > 65536:
        raise ValueError('Oversized worker Metal capability response')
    value = json.loads(result.stdout)
    if value.get('ort_version') != '1.31.0' or value.get('gpu_execution_verified') is not False:
        raise ValueError('Unexpected runtime/capability GPU claim')
    return value


def decode_response(data):
    if len(data) < 4:
        raise ValueError('Truncated capture response')
    length, = struct.unpack('<I', data[:4])
    if not 0 < length <= 16 * 1024 * 1024 or len(data) != length + 4:
        raise ValueError('Expected exactly one bounded framed capture response')
    return json.loads(data[4:])


def run(args):
    root, worker, identity = inputs(args)
    output = args.output.resolve()
    protected = args.bundle_manifest.resolve(strict=True).parent if args.bundle_manifest else root
    if output.is_relative_to(protected) or protected.is_relative_to(output):
        raise ValueError('Capture evidence must be independent of the immutable native payload')
    output = diag.new_directory(output)
    report = {'schema_version': 1, **identity, 'gpu_execution_verified': False,
              'gpu_speedup': None, 'timing_eligible': False, 'permission_changes_attempted': False,
              'scope': 'Actual worker binary, explicit capture only. A trace file, CoreML EP profile '
                       'or submitted commands do not certify actual GPU execution; review attribution '
                       'and execution evidence. All capture timings are excluded from benchmarks.'}
    try:
        report['capabilities'] = capabilities(worker)
        if args.command == 'probe':
            report['result'] = 'capability-only'
        else:
            if not report['capabilities'].get('metal_capture', {}).get('available'):
                raise ValueError('Metal capture unavailable; no alternative profiler attempted')
            goldens, selected = diag.selected_cases([args.case])
            _, case = selected[0]
            request = {'id': 1, 'provider': args.provider, 'tier': case['tier'],
                       'rec_model': case['rec_model'], 'operation': 'ocr', 'path': case['image'],
                       'cpu_threads': args.threads, 'batch_size': args.batch_size, 'timeout_ms': 120000}
            body = json.dumps(request, ensure_ascii=False).encode('utf-8')
            command = [str(worker), '--models', str(root / 'models'), '--config', str(root / 'config'),
                       '--diagnostic-metal-capture', str(output / 'metal-evidence')]
            report.update({'command': command, 'case_index': args.case, 'request': request,
                           'fixture_sha256': case['fixture_sha256']})
            diag.write_json(output / 'request.json', request)
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=environment()) as child:
                report['observed_worker_pid'] = child.pid
                try:
                    stdout, stderr = child.communicate(struct.pack('<I', len(body)) + body, timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    child.kill()
                    stdout, stderr = child.communicate()
                    (output / 'worker.stderr.txt').write_bytes(stderr)
                    (output / 'worker.response.bin').write_bytes(stdout)
                    raise
            (output / 'worker.stderr.txt').write_bytes(stderr)
            (output / 'worker.response.bin').write_bytes(stdout)
            report['worker_exit_code'] = child.returncode
            response = decode_response(stdout)
            report['response'] = response
            if child.returncode != 0 or response.get('ok') is not True:
                raise ValueError('Production worker capture failed; no CPU retry')
            if response.get('id') != 1 or response.get('timing_eligible') is not False:
                raise ValueError('Capture response identity/timing contract mismatch')
            from backend.tools.native_ocr import validate_execution_evidence
            report['execution_evidence'] = validate_execution_evidence(response, request, worker_pid=child.pid)
            report['frozen_golden_parity'] = diag.pipeline_parity([response['result']], [case['expected']],
                                                                 goldens['confidence_tolerance'])
            capture = response.get('diagnostic_metal_capture', {})
            if capture.get('gpu_execution_verified') is not False:
                raise ValueError('Capture configuration must not claim GPU verification')
            trace = output / 'metal-evidence' / 'metal-capture.gputrace'
            entries = sorted(trace.rglob('*')) if trace.is_dir() else [trace]
            if trace.is_symlink() or any(path.is_symlink() for path in entries):
                raise ValueError('Refusing to read links in captured trace')
            report['trace_files'] = [{'file': str(path.relative_to(output)), 'sha256': diag.digest(path),
                                      'bytes': path.stat().st_size} for path in entries if path.is_file()]
            report['trace_files_present'] = bool(report['trace_files'])
            report['result'] = 'collected-not-gpu-certified'
        # Detect any file drift across execution without updating the manifest.
        _, _, after = inputs(args)
        if after != identity:
            raise ValueError('Worker/runtime/model inputs changed during capture')
    except Exception as error:
        report.update(result='failed', error=str(error))
        diag.write_json(output / 'production-capture-report.json', report)
        raise
    diag.write_json(output / 'production-capture-report.json', report)
    print(output / 'production-capture-report.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('probe', 'capture'))
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--worker-sha256', required=True, help='Trusted build/final ZIP executable SHA256')
    parser.add_argument('--bundle-manifest', type=Path, help='Bind actual final-bundle native payload')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', type=int, default=0)
    parser.add_argument('--provider', choices=('cpu', 'coreml'), default='coreml')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--batch-size', type=int, default=6)
    parser.add_argument('--timeout', type=int, default=240)
    run(parser.parse_args(argv))


if __name__ == '__main__':
    main()
