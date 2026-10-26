"""Source-contract guards; actual CoreML device execution requires Mac evidence."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest

import build

ROOT = Path(__file__).resolve().parents[1]


def test_coreml_is_explicit_mac_only_and_does_not_enable_ane():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    eligibility = source[source.index('bool coreml_available'):source.index('void require_provider')]
    assert '#ifdef __APPLE__' in eligibility
    assert '"1.31.0"' in eligibility
    assert '"CoreMLExecutionProvider"' in eligibility
    assert 'const char *values[] = {"MLProgram", "CPUAndGPU", "0", "0", "0"}' in source
    assert '"CPUAndNeuralEngine"' not in source
    assert 'SessionOptionsAppendExecutionProvider(o, "CoreML"' in source


def test_legacy_cpu_calls_remain_cpu_and_engine_cache_includes_provider():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    assert source.count('const std::string &requested_provider = "cpu"') == 2
    assert 'request.value("provider", std::string("cpu"))' in source
    assert '":" + operation + ":" + provider' in source
    assert 'rec_path, threads, provider' in source
    assert 'result["execution_evidence"] = engine->provider_evidence()' in source


def test_capabilities_dont_require_models_or_claim_gpu_execution():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    main = source[source.index('int wmain'):]
    assert main.index('provider_capabilities().dump()') < main.index('if (models.empty() || config.empty())')
    assert '"default_request_provider", "cpu"' in source
    assert '"supported_request_providers", supported' in source
    helper = (ROOT / 'provider_evidence.hpp').read_text(encoding='utf-8')
    assert '"gpu_execution_verified", false' in helper
    assert '"gpu_execution_verified", true' not in helper


def test_worker_requires_optout_before_any_ort_api_use():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    main = source[source.index('int wmain'):]
    assert main.index('const char *telemetry_optout') < main.index('OrtGetApiBase()->GetApi')
    assert 'if (!telemetry_optout || std::string(telemetry_optout) != "1")' in main
    assert main.index('DisableTelemetryEvents(env)') > main.index('CreateEnv(')


def test_cached_first_profile_is_not_mislabeled_as_current_blank_execution():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    assert 'det.request_run_count = 0' in source
    assert 'rec->request_run_count = 0' in source
    assert 'J evidence = request_run_count ? execution_evidence' in source
    assert 'evidence["evidence_reused"]' in source
    assert '"profile_window"' in source and '"execution_window"' in source
    assert '"monotonic_clock", "std::chrono::steady_clock"' in source
    assert 'first successful OrtRun; ORT EP profile only, not GPU hardware attribution' in source


def test_first_profile_is_private_bounded_and_all_cpu_is_rejected():
    source = (ROOT / 'worker.cpp').read_text(encoding='utf-8')
    assert 'mkdtemp(buffer.data())' in source
    assert 'fs::is_symlink(path)' in source
    assert 'fs::canonical(path.parent_path()) != profile_directory' in source
    assert 'fs::file_size(path) > 16 * 1024 * 1024' in source
    assert 'depth > 32' in source
    assert 'SessionEndProfiling(s, allocator, &name)' in source
    assert 'if (!provider_error.empty())' in source
    helper = (ROOT / 'provider_evidence.hpp').read_text(encoding='utf-8')
    assert 'if (!coreml)' in helper and 'all-CPU fallback is refused' in helper
    assert 'CoreML internal CPU/GPU dispatch' in helper


def test_new_compiled_helper_has_provenance_and_tests_are_not_packaged():
    source = (ROOT / 'build.py').read_text(encoding='utf-8')
    assert "'provider_evidence_sha256': sha256(ROOT / 'provider_evidence.hpp')" in source
    cmake = (ROOT / 'CMakeLists.txt').read_text(encoding='utf-8')
    assert 'VSE_OCR_BUILD_PROVIDER_TESTS' in cmake
    assert 'never packaged' in cmake


def capabilities():
    return {'schema_version': 1, 'ort_version': '1.31.0',
            'available_providers': ['CPUExecutionProvider'],
            'supported_request_providers': ['cpu'], 'default_request_provider': 'cpu',
            'gpu_execution_verified': False,
            'coreml': {'available': False, 'model_format': 'MLProgram',
                       'compute_units': 'CPUAndGPU', 'ane_allowed': False,
                       'gpu_execution_verified': False}}


def test_build_probe_optout_is_set_before_process_creation(tmp_path, monkeypatch):
    (tmp_path / 'native-ocr').write_bytes(b'worker fixture; never executed')
    (tmp_path / 'libonnxruntime.so.1').write_bytes(b'runtime fixture; never loaded')
    monkeypatch.setenv('ORT_DISABLE_TELEMETRY', '0')
    monkeypatch.setenv('LD_PRELOAD', '/untrusted')
    monkeypatch.setenv('DYLD_LIBRARY_PATH', '/untrusted')

    def run(command, **kwargs):
        assert command == [str(tmp_path / 'native-ocr'), '--capabilities']
        assert kwargs['env']['ORT_DISABLE_TELEMETRY'] == '1'
        assert 'LD_PRELOAD' not in kwargs['env']
        assert 'DYLD_LIBRARY_PATH' not in kwargs['env']
        assert kwargs['timeout'] == 30 and kwargs['check']
        return SimpleNamespace(stdout=json.dumps(capabilities()))

    monkeypatch.setattr(build.subprocess, 'run', run)
    provenance = build.record_capabilities(tmp_path, 'native-ocr', 'libonnxruntime.so.1')
    path = tmp_path / 'native-capabilities.json'
    assert json.loads(path.read_text()) == capabilities()
    assert provenance['path'] == path.name
    assert provenance['sha256'] == build.sha256(path)
    assert provenance['worker_sha256'] == build.sha256(tmp_path / 'native-ocr')
    assert provenance['runtime_sha256'] == build.sha256(tmp_path / 'libonnxruntime.so.1')
    assert provenance['telemetry_disabled_before_exec'] is True


@pytest.mark.parametrize('mutation', [
    {'ort_version': '1.20.1'}, {'gpu_execution_verified': True},
    {'supported_request_providers': ['cpu', 'coreml']},
    {'coreml': {'available': False, 'compute_units': 'ALL'}},
    {'available_providers': []},
])
def test_build_probe_rejects_wrong_or_inconsistent_capabilities(tmp_path, monkeypatch, mutation):
    report = {**capabilities(), **mutation}
    monkeypatch.setattr(build.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=json.dumps(report)))
    with pytest.raises(ValueError):
        build.record_capabilities(tmp_path, 'native-ocr', 'libonnxruntime.so.1')
    assert not (tmp_path / 'native-capabilities.json').exists()
