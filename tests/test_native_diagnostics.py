"""Independent diagnostics must never certify a GPU from provider/config names."""
from array import array
import json
from pathlib import Path
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
    assert "('worker.cpp', 'provider_evidence.hpp')" in source


def test_metal_capture_environment_is_explicit_and_never_in_timing(monkeypatch):
    monkeypatch.setenv('MTL_CAPTURE_ENABLED', '1')
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    assert 'MTL_CAPTURE_ENABLED' not in diag.diagnostic_environment()
    assert diag.diagnostic_environment()['ORT_DISABLE_TELEMETRY'] == '1'
    assert diag.diagnostic_environment(True)['MTL_CAPTURE_ENABLED'] == '1'


def test_metal_capture_is_private_evidence_not_gpu_certification():
    source = (diag.HERE / 'metal_capture.mm').read_text()
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
    assert 'request.value("warmup", 0) != 0' in runner
    assert 'request.value("repeats", 0) != 0' in runner
    assert 'CaptureGuard' in runner
    assert '"scope", "synchronous OrtRun"' in runner
    assert 'metal_capture' not in (diag.ROOT / 'native/ocr/CMakeLists.txt').read_text()


def test_metal_capture_missing_capability_refuses_before_output(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, 'verify_models', lambda _: ({}, {}))
    monkeypatch.setattr(diag, 'selected_cases', lambda _: ({}, [(0, {})]))
    monkeypatch.setattr(diag, 'metal_probe', lambda _: {'metal_capture': {'available': False}})
    args = SimpleNamespace(runner=tmp_path, artifact=tmp_path, case=0, output=tmp_path / 'output')
    with pytest.raises(ValueError, match='no alternative profiler'):
        diag.capture_case(args)
    assert not args.output.exists()
