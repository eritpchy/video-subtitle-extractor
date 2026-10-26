"""Independent diagnostics must never certify a GPU from provider/config names."""
from array import array
import json
import os
from pathlib import Path
import struct
import subprocess
from types import SimpleNamespace

import pytest

from scripts.native_diagnostics import benchmark as diag


def test_statistics_median_dispersion_and_invalid_data():
    report = diag.summarize([1., 2., 3., 4., 100.])
    assert report['median_seconds'] == 3
    assert report['mad_seconds'] == 1
    assert report['p25_seconds'] == 2 and report['p75_seconds'] == 4
    for bad in ([], [-1], [float('nan')], [float('inf')]):
        with pytest.raises(ValueError):
            diag.summarize(bad)


def test_existing_output_and_symlink_are_refused(tmp_path):
    target = tmp_path / 'output'
    diag.new_directory(target)
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        diag.new_directory(target)
    link = tmp_path / 'link'
    link.symlink_to(tmp_path / 'missing')
    with pytest.raises(FileExistsError):
        diag.new_directory(link)


def test_coreml_and_cpu_fallback_are_not_gpu_proof(tmp_path):
    path = tmp_path / 'profile.json'
    diag.write_json(path, [
        {'cat': 'Node', 'name': 'fused_kernel_time', 'dur': 50, 'args': {'provider': 'CoreMLExecutionProvider'}},
        {'cat': 'Node', 'name': 'shape_kernel_time', 'dur': 10, 'args': {'provider': 'CPUExecutionProvider'}},
        {'cat': 'Node', 'name': 'fused_fence_before', 'dur': 100, 'args': {'provider': 'CoreMLExecutionProvider'}},
        {'cat': 'Session', 'dur': 1000, 'args': {}}])
    report = diag.profile_evidence(tmp_path, [{'model': 'm.onnx', 'profile': path.name}], 'coreml-gpu-permitted')
    assert report['coreml_ep_execution_observed']
    assert report['cpu_ep_fallback_observed']
    assert report['kernel_duration_microseconds']['CoreMLExecutionProvider'] == 50
    assert report['gpu_execution_verified'] is False
    cpu = diag.profile_evidence(tmp_path, [{'model': 'm.onnx', 'profile': path.name}])
    assert not cpu['cpu_ep_fallback_observed']


def test_no_profile_means_no_execution_evidence(tmp_path):
    result = diag.profile_evidence(tmp_path, [{'model': 'm', 'profile': None}], 'coreml-gpu-permitted')
    assert not result['coreml_ep_execution_observed']
    assert not result['gpu_execution_verified']


def test_pipeline_parity_keeps_exact_boxes_text_and_frozen_tolerance():
    expected = [[[[[0, 0], [2, 0], [2, 2], [0, 2]]], [['Text', .9]]]]
    actual = json.loads(json.dumps(expected))
    actual[0][1][0][1] += .000001
    assert diag.pipeline_parity(actual, expected)['passed']
    actual[0][1][0][1] += .001
    assert not diag.pipeline_parity(actual, expected)['passed']
    actual = json.loads(json.dumps(expected))
    actual[0][0][0][0][0] = 1
    assert not diag.pipeline_parity(actual, expected)['passed']
    actual = json.loads(json.dumps(expected))
    actual[0][1][0][1] = float('nan')
    assert not diag.pipeline_parity(actual, expected)['passed']


def test_tensor_parity_finite_outputs_and_diagnostic_tolerance(tmp_path):
    a, b = tmp_path / 'a.f32', tmp_path / 'b.f32'
    a.write_bytes(array('f', [1, 2, 3]).tobytes())
    b.write_bytes(array('f', [1, 2, 3]).tobytes())
    assert diag.tensor_parity(a, b)['exact_sha256_equal']
    b.write_bytes(array('f', [1, 2, float('inf')]).tobytes())
    assert not diag.tensor_parity(a, b)['passed']


def test_pins_are_required_and_aliases_cannot_hide_changed_bytes(tmp_path):
    path = tmp_path / 'file'
    path.write_bytes(b'original')
    expected = diag.digest(path)
    assert diag.pinned_file(path, expected)['bytes'] == 8
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(ValueError):
        diag.pinned_file(link, expected)
    path.write_bytes(b'changed')
    with pytest.raises(ValueError):
        diag.pinned_file(path, expected)


def test_default_fixture_selection_exercises_all_recognizers_and_two_detectors():
    _, cases = diag.selected_cases()
    pins = diag.read_json(diag.ROOT / 'native/ocr/model-source-pins.json')
    covered = {case['rec_model'] for _, case in cases}
    covered.update('PP-OCRv6_' + case['tier'] + '_det' for _, case in cases)
    assert covered == {row['name'] for row in pins['models']}
    assert len(covered) == 15


def test_diagnostic_is_separate_from_production_and_never_uses_ane():
    root = diag.ROOT
    source = (root / 'scripts/native_diagnostics/runner.cpp').read_text()
    assert '"CPUOnly" : "CPUAndGPU"' in source
    assert '"CPUAndNeuralEngine"' not in source and '"ALL"' not in source
    assert 'SessionOptionsAppendExecutionProvider(copy, "CoreML"' in source
    assert 'CloneSessionOptions(options, &copy)' in source
    assert 'CPU substitution refused' in source
    # The separately authorized production CoreML rollout owns its own guards.
    # These diagnostics must not depend on production remaining CPU-only.
    assert '#define main unused_production_worker_main' in source
    assert 'api = &wrapped' in source
    assert 'native_diagnostics' not in (root / 'native/ocr/CMakeLists.txt').read_text()


def test_process_statistics_do_not_claim_gpu_memory_or_video_time():
    result = diag.aggregate_runs([{'status': 'failed'}])
    assert result['completed_processes'] == 0
    result = diag.aggregate_runs([{'status': 'completed', 'samples': [{'wall_seconds': .5}, {'wall_seconds': 1.5}],
                                  'wall_seconds_summary': {'median_seconds': 1.},
                                  'process_entry_to_first_prediction_seconds': 2., 'max_rss_bytes': 1234}])
    assert result['inputs_per_second'] == 1.
    assert result['gpu_memory_bytes'] is None


def test_telemetry_is_disabled_before_exec_not_just_after_createenv(monkeypatch):
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    monkeypatch.setenv('DIAGNOSTIC_TEST_EXISTING_VALUE', 'unchanged')
    env = diag.offline_runtime_environment()
    assert env['ORT_DISABLE_TELEMETRY'] == '1'
    assert env['DIAGNOSTIC_TEST_EXISTING_VALUE'] == 'unchanged'
    source = (diag.HERE / 'runner.cpp').read_text()
    main = source[source.index('int main(int argc, char **argv)'):]
    assert main.index('std::getenv("ORT_DISABLE_TELEMETRY")') < main.index('original = OrtGetApiBase()')
    assert 'IsRunningInCI' not in source


def test_video_roi_and_timestamp_selection_fail_closed():
    from scripts.native_diagnostics import video
    assert video.crop_values('0.6,1,0,1') == (.6, 1, 0, 1)
    for value in ('1,0,0,1', 'nan,1,0,1', '0,1,0,2', '0,1,1'):
        with pytest.raises(ValueError):
            video.crop_values(value)
    result = SimpleNamespace(returncode=0, stdout='Frame: 0_0_2_500__x\nFrame: 0_0_0_0__y\nFrame: 0_0_2_500__z\nFINISH\n')
    assert video.parse_vsf_result(result) == [0, 2500]
    for text in ('FINISH\n', 'Frame: 0_0_0_0__x\n', 'Cannot decode the first frame\nFINISH\n'):
        with pytest.raises(ValueError):
            video.parse_vsf_result(SimpleNamespace(returncode=0, stdout=text))


def test_video_diagnostic_has_no_implicit_network_decoder_or_gpu_claim():
    source = (diag.HERE / 'video.py').read_text()
    assert source.count("'-protocol_whitelist', 'file,pipe'") == 2
    assert 'use_cuda=False' in source
    assert "'production_video_end_to_end_seconds': None" in source
    assert "'gpu_speedup': None" in source
    assert 'comparison refused' in source


def test_minimal_runtime_probe_requires_optout_before_dynamic_load():
    source = (diag.HERE / 'runtime_probe.cpp').read_text()
    assert source.index('std::getenv("ORT_DISABLE_TELEMETRY")') < source.index('dlopen(')
    assert 'api->GetAvailableProviders' in source
    assert 'CreateEnv(' not in source and 'CreateSession(' not in source
    assert '#include <opencv' not in source.lower()


def test_build_provenance_covers_production_provider_helper():
    source = (diag.HERE / 'benchmark.py').read_text()
    assert "'production_included_source_sha256'" in source
    for name in ('worker.cpp', 'provider_evidence.hpp', 'protocol_output.hpp', 'input_shape.hpp', 'metal_capture.hpp',
                 'metal_capture.mm', 'metal_capture_stub.cpp'):
        assert repr(name) in source
    runner = (diag.HERE / 'runner.cpp').read_text()
    assert runner.index('native_protocol::Output protocol;') < runner.index('original = OrtGetApiBase()')
    assert 'protocol.capabilities(info.dump(2))' in runner
    assert 'std::cout' not in runner


def test_runtime_sidecar_is_pinned_and_named_for_portable_loader(tmp_path):
    sdk, output = tmp_path / 'sdk', tmp_path / 'output'
    sdk.mkdir(); output.mkdir()
    source = sdk / 'libonnxruntime.1.31.0.dylib'; source.write_bytes(b'pinned-runtime')
    record = {'library': source.name, 'files': [
        {'path': source.name, 'role': 'runtime', 'sha256': diag.digest(source)}]}
    result = diag.stage_runtime(sdk, record, 'macos-arm64', output)
    assert result[0]['file'] == 'libonnxruntime.1.dylib'
    assert (output / result[0]['file']).read_bytes() == source.read_bytes()
    source.write_bytes(b'changed')
    with pytest.raises(ValueError):
        diag.stage_runtime(sdk, record, 'macos-arm64', output)


def test_bundled_runtime_retains_pinned_license_and_notices(tmp_path):
    sdk, output = tmp_path / 'sdk', tmp_path / 'output'
    sdk.mkdir(); output.mkdir()
    records = []
    for name in ('LICENSE', 'ThirdPartyNotices.txt', 'Privacy.md'):
        path = sdk / name; path.write_text('original pinned ' + name)
        records.append({'path': name, 'role': 'notice', 'sha256': diag.digest(path)})
    copied = diag.stage_runtime_notices(sdk, {'files': records}, output)
    assert {row['file'] for row in copied} == {'ort-notices/' + row['path'] for row in records}
    assert all(diag.digest(output / row['file']) == row['sha256'] for row in copied)


def test_macos_diagnostic_rejects_absolute_ci_rpath_or_other_dependencies():
    deps = 'worker:\n\t@rpath/libonnxruntime.1.dylib (compatibility version 0.0.0)\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n'
    commands = 'Load command 1\n          cmd LC_RPATH\n      cmdsize 32\n         path @loader_path (offset 12)\n'
    assert diag.validate_macos_linkage(deps, commands)['rpaths'] == ['@loader_path']
    for bad_deps, bad_commands in ((deps, commands.replace('@loader_path', '/Users/runner/cache')),
                                   (deps + '\t/opt/local/libbad.dylib (compatibility version 0.0.0)\n', commands),
                                   (deps, commands + commands)):
        with pytest.raises(ValueError):
            diag.validate_macos_linkage(bad_deps, bad_commands)


def test_relocation_probe_uses_copied_runtime_and_cleans_loader_environment(tmp_path, monkeypatch):
    binary = tmp_path / 'runner'; binary.write_bytes(b'runner')
    runtime = tmp_path / 'libonnxruntime.1.dylib'; runtime.write_bytes(b'runtime')
    monkeypatch.setenv('DYLD_LIBRARY_PATH', '/untrusted')
    monkeypatch.setenv('MTL_CAPTURE_ENABLED', '1')
    def launch(command, **options):
        copied = Path(command[0])
        assert copied.parent != tmp_path and copied.read_bytes() == binary.read_bytes()
        assert 'DYLD_LIBRARY_PATH' not in options['env'] and 'MTL_CAPTURE_ENABLED' not in options['env']
        assert options['env']['ORT_DISABLE_TELEMETRY'] == '1'
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'ort_version': '1.31.0', 'loaded_ort_library': str(copied.parent / runtime.name)}))
    monkeypatch.setattr(diag.subprocess, 'run', launch)
    report = diag.relocated_runtime_probe(binary, [{'file': runtime.name, 'sha256': diag.digest(runtime)}])
    assert report['passed'] and report['loader_environment_override'] is False


def test_metal_capture_environment_is_explicit_and_never_in_timing(monkeypatch):
    monkeypatch.setenv('MTL_CAPTURE_ENABLED', '1')
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    assert 'MTL_CAPTURE_ENABLED' not in diag.diagnostic_environment()
    assert diag.diagnostic_environment()['ORT_DISABLE_TELEMETRY'] == '1'
    assert diag.diagnostic_environment(True)['MTL_CAPTURE_ENABLED'] == '1'


def test_metal_capture_is_private_evidence_not_gpu_certification():
    source = (diag.ROOT / 'native/ocr/metal_capture.mm').read_text()
    assert 'MTLCaptureDestinationGPUTraceDocument' in source
    assert 'supportsDestination:' in source
    assert 'descriptor.captureObject = device' in source
    assert 'descriptor.outputURL' in source
    assert 'coreml_device_object_match_verified", false' in source
    assert 'gpu_execution_verified", false' in source
    assert 'DeveloperTools' not in source and 'xctrace' not in source
    assert 'system(' not in source and 'chmod' not in source
    runner = (diag.HERE / 'runner.cpp').read_text()
    assert 'Metal capture is single-pass evidence only' in runner
    assert 'warmups != 0 || repeats != 0' in runner
    assert runner.index('const int repeats = bounded_int') < runner.index('Metal capture is single-pass evidence only')
    assert 'CaptureGuard' in runner
    assert '"scope", "synchronous OrtRun"' in runner


def test_metal_capture_missing_capability_refuses_before_output(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, 'verify_models', lambda _: ({}, {}))
    monkeypatch.setattr(diag, 'selected_cases', lambda _: ({}, [(0, {})]))
    monkeypatch.setattr(diag, 'metal_probe', lambda _: {'metal_capture': {'available': False}})
    args = SimpleNamespace(runner=tmp_path, artifact=tmp_path, case=0, output=tmp_path / 'output')
    with pytest.raises(ValueError, match='no alternative profiler'):
        diag.capture_case(args)
    assert not args.output.exists()


def test_production_capture_requires_exactly_one_bounded_response():
    from scripts.native_diagnostics import production_capture as production
    body = b'{"ok":true,"id":1}'
    framed = struct.pack('<I', len(body)) + body
    assert production.decode_response(framed) == {'ok': True, 'id': 1}
    for invalid in (b'', framed[:-1], framed + framed, struct.pack('<I', 0), struct.pack('<I', 17000000)):
        with pytest.raises(ValueError):
            production.decode_response(invalid)


def test_production_capture_has_explicit_optout_and_no_loader_injection(monkeypatch):
    from scripts.native_diagnostics import production_capture as production
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH'):
        monkeypatch.setenv(key, '/untrusted')
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    values = production.environment()
    assert values['ORT_DISABLE_TELEMETRY'] == values['MTL_CAPTURE_ENABLED'] == '1'
    assert values['VSE_OCR_PARENT_PID'] == str(os.getpid())
    for key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH'):
        assert key not in values
        assert key not in diag.diagnostic_environment()


def test_production_capture_refuses_output_inside_payload_before_creation(tmp_path, monkeypatch):
    from scripts.native_diagnostics import production_capture as production
    root = tmp_path / 'payload'; root.mkdir()
    output = root / 'forbidden-output'
    monkeypatch.setattr(production, 'inputs', lambda _: (root, root / 'native-ocr', {}))
    with pytest.raises(ValueError, match='independent'):
        production.run(SimpleNamespace(output=output, bundle_manifest=None))
    assert not output.exists()


def test_production_capture_missing_capability_cannot_start_worker(tmp_path, monkeypatch):
    from scripts.native_diagnostics import production_capture as production
    root = tmp_path / 'payload'; root.mkdir()
    output = tmp_path / 'evidence'
    monkeypatch.setattr(production, 'inputs', lambda _: (root, root / 'native-ocr', {}))
    monkeypatch.setattr(production, 'capabilities', lambda _: {'metal_capture': {'available': False}})
    monkeypatch.setattr(production.subprocess, 'Popen', lambda *a, **k: pytest.fail('capture must not start'))
    with pytest.raises(ValueError, match='no alternative profiler'):
        production.run(SimpleNamespace(output=output, bundle_manifest=None, command='capture'))
    report = diag.read_json(output / 'production-capture-report.json')
    assert report['result'] == 'failed' and report['gpu_execution_verified'] is False


def test_production_capture_collection_never_certifies_gpu_or_timing(tmp_path, monkeypatch):
    from scripts.native_diagnostics import production_capture as production
    from backend.tools import native_ocr
    root = tmp_path / 'payload'; root.mkdir()
    output = tmp_path / 'evidence'
    expected = [[], []]
    case = {'tier': 'small', 'rec_model': 'PP-OCRv6_small_rec', 'image': '/fixed/blank.png',
            'fixture_sha256': 'a' * 64, 'expected': expected}
    monkeypatch.setattr(production, 'inputs', lambda _: (root, root / 'native-ocr', {}))
    monkeypatch.setattr(production, 'capabilities', lambda _: {'metal_capture': {'available': True}})
    monkeypatch.setattr(diag, 'selected_cases', lambda _: ({'confidence_tolerance': 1e-5}, [(0, case)]))
    response = {'id': 1, 'ok': True, 'timing_eligible': False, 'result': expected,
                'diagnostic_metal_capture': {'gpu_execution_verified': False}}
    body = json.dumps(response).encode()

    class Child:
        pid = 4242
        returncode = 0
        def __init__(self, command, **kwargs):
            assert command[-2:] == ['--diagnostic-metal-capture', str(output / 'metal-evidence')]
            assert kwargs['env']['MTL_CAPTURE_ENABLED'] == kwargs['env']['ORT_DISABLE_TELEMETRY'] == '1'
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, data, timeout):
            request = production.decode_response(data)
            assert request['id'] == 1 and request['path'] == case['image']
            return struct.pack('<I', len(body)) + body, b''

    monkeypatch.setattr(production.subprocess, 'Popen', Child)
    def validate(response, request, *, worker_pid):
        assert worker_pid == 4242
        return {'gpu_execution_verified': False}
    monkeypatch.setattr(native_ocr, 'validate_execution_evidence', validate)
    production.run(SimpleNamespace(output=output, bundle_manifest=None, command='capture', case=0,
                                   provider='coreml', threads=4, batch_size=6, timeout=60))
    report = diag.read_json(output / 'production-capture-report.json')
    assert report['result'] == 'collected-not-gpu-certified'
    assert not report['gpu_execution_verified'] and not report['timing_eligible']
    assert not report['trace_files_present'] and report['gpu_speedup'] is None
    assert report['observed_worker_pid'] == 4242 and report['frozen_golden_parity']['passed']


def test_compiled_diagnostic_capture_cannot_omit_repeat_limits(tmp_path):
    value = os.environ.get('VSE_DIAGNOSTIC_TEST_RUNNER')
    if not value:
        pytest.skip('Set VSE_DIAGNOSTIC_TEST_RUNNER to a verified compiled diagnostic')
    request = tmp_path / 'request.json'
    diag.write_json(request, {'schema_version': 1, 'mode': 'ort-cpu', 'evidence': True, 'metal_capture': True})
    result = subprocess.run([str(Path(value).resolve(strict=True)), str(request), str(tmp_path / 'output')],
                            capture_output=True, text=True, timeout=15, env=diag.diagnostic_environment(True))
    assert result.returncode == 2
    assert 'Metal capture is single-pass evidence only' in result.stderr
    assert not (tmp_path / 'output' / 'result.json').exists()
