#!/usr/bin/env python3
"""Reproducible native CPU worker assembly. Build tools never enter runtime.

This helper certifies pinned inputs, not final platform dependency closure. The
outer application packager must run attributable runtime/clean-client audits.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

from fetch_sources import fetch as fetch_sources, relative, sha256
from fetch_ort import fetch as fetch_ort
from build_opencv import build as build_opencv

ROOT = Path(__file__).resolve().parent
TARGETS = {'linux-x64': ('Linux', {'x86_64', 'amd64'}),
           'windows-x64': ('Windows', {'amd64', 'x86_64'}),
           'macos-arm64': ('Darwin', {'arm64', 'aarch64'})}


def native_target(target):
    if target not in TARGETS:
        raise ValueError('Unsupported native target: ' + target)
    system, machines = TARGETS[target]
    if platform.system() != system or platform.machine().lower() not in machines:
        raise RuntimeError('Cross-compilation is not certification; build on native ' + target)


def verify_models(root):
    root = Path(root).resolve(strict=True)
    rows = json.loads((root / 'model-manifest.json').read_text(encoding='utf-8'))
    pins = json.loads((ROOT / 'model-source-pins.json').read_text(encoding='utf-8'))
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


def opencv_config(prefix):
    for p in [prefix / 'lib/cmake/opencv4', prefix]:
        if (p / 'OpenCVConfig.cmake').is_file():
            return p
    matches = list(prefix.rglob('OpenCVConfig.cmake'))
    if len(matches) != 1:
        raise RuntimeError('Cannot unambiguously locate static OpenCV CMake configuration')
    return matches[0].parent


def copy_notices(source, sdk, stage, sdk_record):
    dest = stage / 'licenses'
    dest.mkdir()
    for row in sdk_record['files']:
        if row['role'] == 'notice':
            shutil.copy2(sdk / row['path'], dest / ('onnxruntime-' + Path(row['path']).name))
    sources = {
        'json-LICENSE.MIT': source / 'json-LICENSE.MIT',
        'clipper-LICENSE.BOOST': source / 'clipper-LICENSE.BOOST',
        'pyclipper-LICENSE': source / 'pyclipper-LICENSE',
        'opencv-LICENSE': source / 'opencv-4.10.0/LICENSE',
        'libjpeg-turbo-LICENSE.md': source / 'opencv-4.10.0/3rdparty/libjpeg-turbo/LICENSE.md',
        'libjpeg-README.ijg': source / 'opencv-4.10.0/3rdparty/libjpeg-turbo/README.ijg',
        'libpng-LICENSE': source / 'opencv-4.10.0/3rdparty/libpng/LICENSE',
        'zlib-LICENSE': source / 'opencv-4.10.0/3rdparty/zlib/LICENSE',
        'PaddlePaddle-Apache-2.0.txt': ROOT / 'LICENSE',
    }
    for name, path in sources.items():
        if not path.is_file():
            raise FileNotFoundError('Required dependency license missing: ' + str(path))
        shutil.copy2(path, dest / name)
    (dest / 'PaddlePaddle-NOTICE.txt').write_text(
        'OCR frontend derived from PaddlePaddle PaddleX 3.7.2; copyright (c) 2024 PaddlePaddle Authors. '
        'Apache-2.0. Exact upstream source hashes: source_manifest.json. '
        'Model archive/source provenance: model-source-pins.json and model-manifest.json.\n', encoding='utf-8')


def vc_runtime(directory, license_file, stage, required, acknowledged=False):
    if directory is None and license_file is None:
        return {'complete': False, 'required': required,
                'reason': 'No licensed Visual C++ redistributable source supplied; final closure must fail until resolved.'}
    if not acknowledged:
        raise PermissionError('Applicable Microsoft redistribution rights must be explicitly confirmed')
    if not directory or not license_file:
        raise ValueError('Both --vc-runtime-dir and --vc-license are required')
    from vc_runtime_helper import verify_source
    manifest, _ = verify_source(directory, license_file)
    directory, license_file = Path(directory).resolve(strict=True), Path(license_file).resolve(strict=True)
    paths = {p.name.lower(): p for p in directory.iterdir() if p.is_file() and not p.is_symlink()}
    records = []
    for name in required:
        p = paths.get(name.lower())
        if not p:
            raise FileNotFoundError('Required redistributable DLL missing: ' + name)
        shutil.copy2(p, stage / name)
        records.append({'source': str(p), 'file': name, 'sha256': sha256(p), 'bytes': p.stat().st_size})
    shutil.copy2(license_file, stage / 'licenses/Visual-Cpp-redistributable-license.txt')
    return {'complete': True, 'files': records, 'license_sha256': sha256(license_file), 'official_source': manifest['installer'],
            'scope': 'Caller-supplied licensed redistributable source; final PE/ownership audit remains required.'}


def inventory(stage, target, provenance):
    files = [{'path': p.relative_to(stage).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)}
             for p in sorted(stage.rglob('*')) if p.is_file() and not p.is_symlink()]
    payload = sum(r['bytes'] for r in files)
    report = {'schema_version': 1, 'target': target, 'release_ready': False,
              'pending_gates': ['attributable platform compiler/runtime closure', 'native platform smoke/parity',
                                'clean network-disabled client launch without host Python'],
              'files': files, 'runtime_payload_bytes': payload, 'python_runtime_bytes': 0,
              'model_bytes': sum(r['bytes'] for r in files if r['path'].startswith('models/')),
              'provenance': provenance, 'assembled_total_bytes': payload}
    for _ in range(8):
        report['assembled_total_bytes'] = payload + len((json.dumps(report, indent=2) + '\n').encode())
    (stage / 'size-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def build(a):
    target = a.platform + '-' + a.arch
    native_target(target)
    if a.runner_test_only and a.platform != 'windows':
        raise ValueError('--runner-test-only is exclusively a Windows license-pending mode')
    if a.runner_test_only and (a.acknowledge_vc_redistribution_license or a.vc_runtime_dir or a.vc_license):
        raise ValueError('Runner-only mode must not accept or copy VC redistribution inputs')
    if a.platform == 'windows' and not a.runner_test_only and not a.acknowledge_vc_redistribution_license:
        raise PermissionError('VC redistribution entitlement pending: use --runner-test-only for runner-local tests; no customer artifact')
    models = a.models.resolve(strict=True)
    rows = verify_models(models)
    output = a.output.absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Refusing to overwrite output artifact: ' + str(output))
    work = (a.work_dir or output.parent / 'native-build').absolute()
    cache = (a.cache or output.parent / 'native-cache').absolute()
    if work.is_symlink() or cache.is_symlink():
        raise ValueError('Build/cache directory cannot be a symlink')
    work.mkdir(parents=True, exist_ok=True)
    (work / 'tmp').mkdir(exist_ok=True)
    os.environ['TMPDIR'] = str(work / 'tmp')
    source = fetch_sources(work / 'sources', cache / 'sources', a.offline)
    sdk = fetch_ort(target, work / 'ort-sdk', cache / 'sdk', offline=a.offline)
    sdk_manifest = json.loads((ROOT / 'ort-sdk-manifest.json').read_text())
    sdk_record = sdk_manifest['targets'][target]
    prefix = work / 'opencv-install'
    build_opencv(source / 'opencv-4.10.0', work / 'opencv-build', prefix, a.cmake, a.jobs, a.generator)
    worker_build = work / 'worker-build'
    command = [a.cmake, '-S', str(ROOT), '-B', str(worker_build), '-DCMAKE_BUILD_TYPE=Release',
               '-DOpenCV_STATIC=ON', '-DOpenCV_DIR=' + str(opencv_config(prefix)),
               '-DORT_INCLUDE_DIR=' + str(sdk / sdk_record['include']),
               '-DORT_LIBRARY=' + str(sdk / sdk_record['link_library']), '-DOCR_DEPS_DIR=' + str(source)]
    if a.generator:
        command += ['-G', a.generator]
    if a.platform == 'macos':
        command += ['-DCMAKE_OSX_DEPLOYMENT_TARGET=14.0', '-DCMAKE_OSX_ARCHITECTURES=arm64']
    subprocess.run(command, check=True)
    subprocess.run([a.cmake, '--build', str(worker_build), '--config', 'Release', '--parallel', str(a.jobs)], check=True)
    executable = 'native-ocr.exe' if a.platform == 'windows' else 'native-ocr'
    binary = next((p for p in [worker_build / executable, worker_build / 'Release' / executable] if p.is_file()), None)
    if binary is None:
        raise FileNotFoundError('Built worker executable missing')
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.native-artifact-', dir=output.parent))
    try:
        shutil.copy2(binary, stage / executable)
        if a.platform != 'windows':
            subprocess.run(['strip', '-x' if a.platform == 'macos' else '--strip-unneeded', str(stage / executable)], check=True)
        for item in sdk_record['files']:
            if item['role'] != 'runtime':
                continue
            name = Path(item['path']).name
            if item['path'] == sdk_record['library']:
                name = {'linux': 'libonnxruntime.so.1', 'macos': 'libonnxruntime.1.dylib', 'windows': 'onnxruntime.dll'}[a.platform]
            shutil.copy2(sdk / item['path'], stage / name)
        for row in rows:
            for relative_file in ['models/' + row['name'] + '.onnx', row['config_file']]:
                dest = stage / relative_file
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    shutil.copy2(models / relative_file, dest)
        shutil.copy2(models / 'model-manifest.json', stage / 'model-manifest.json')
        for name in ['model-source-pins.json', 'conversion-report.json', 'validation-summary.json']:
            candidate = models / name
            if not candidate.is_file():
                candidate = ROOT / name
            if candidate.is_file():
                shutil.copy2(candidate, stage / name)
        shutil.copy2(ROOT / 'source_manifest.json', stage / 'source_manifest.json')
        shutil.copy2(ROOT / 'ort-sdk-manifest.json', stage / 'ort-sdk-manifest.json')
        dependency = {'native_sources': json.loads((ROOT / 'deps/source-pins.json').read_text()),
                      'onnxruntime': {'version': sdk_manifest['version'], 'target': target, **sdk_record},
                      'build_inputs': {'worker_sha256': sha256(ROOT / 'worker.cpp'),
                                       'cmake_sha256': sha256(ROOT / 'CMakeLists.txt')}}
        (stage / 'dependency-source-manifest.json').write_text(json.dumps(dependency, indent=2) + '\n', encoding='utf-8')
        copy_notices(source, sdk, stage, sdk_record)
        if a.platform == 'windows' and a.acknowledge_vc_redistribution_license and a.vc_runtime_dir is None:
            from vc_runtime_helper import fetch as fetch_vc
            a.vc_runtime_dir = fetch_vc(work / 'vc-runtime', cache / 'vc-runtime', offline=a.offline, acknowledge_license=True)
            a.vc_license = a.vc_runtime_dir / 'license.rtf'
        vc = vc_runtime(a.vc_runtime_dir, a.vc_license, stage, sdk_record.get('required_vc_runtime_dlls', []), a.acknowledge_vc_redistribution_license) if a.platform == 'windows' else None
        provenance = {'cmake': subprocess.check_output([a.cmake, '--version'], text=True).splitlines()[0],
                      'host': platform.platform(), 'ort_archive': sdk_record['archive'],
                      'minimum_macos': sdk_record.get('minimum_macos'), 'vc_runtime': vc}
        if a.runner_test_only:
            provenance['runner_test_only'] = True
            provenance['vc_redistribution_pending'] = True
            (stage / 'RUNNER-TEST-ONLY.json').write_text(json.dumps({'runner_test_only': True, 'vc_redistribution_pending': True, 'customer_distribution_permitted': False}, indent=2) + '\n', encoding='utf-8')
        report = inventory(stage, target, provenance)
        if a.runner_test_only:
            report['runner_test_only'] = True
            report['vc_redistribution_pending'] = True
            for _ in range(8):
                report['assembled_total_bytes'] = report['runtime_payload_bytes'] + len((json.dumps(report, indent=2) + '\n').encode())
            (stage / 'size-audit.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        stage.rename(output)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--platform', required=True, choices=['linux', 'windows', 'macos'])
    p.add_argument('--arch', required=True, choices=['x64', 'arm64'])
    p.add_argument('--models', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--work-dir', type=Path)
    p.add_argument('--cache', type=Path)
    p.add_argument('--cmake', default='cmake')
    p.add_argument('--generator')
    p.add_argument('--jobs', type=int, default=3)
    p.add_argument('--offline', action='store_true')
    p.add_argument('--runner-test-only', action='store_true', help='Windows runner-local testing only; no VC redistribution files and never a customer package')
    p.add_argument('--vc-runtime-dir', type=Path)
    p.add_argument('--vc-license', type=Path)
    p.add_argument('--acknowledge-vc-redistribution-license', action='store_true', help='Distributor explicitly confirms applicable Microsoft redistribution rights; never inferred from runner installation')
    a = p.parse_args()
    if not 1 <= a.jobs <= 64:
        p.error('--jobs must be 1..64')
    r = build(a)
    print(json.dumps({k: v for k, v in r.items() if k != 'files'}, indent=2))

if __name__ == '__main__':
    main()
