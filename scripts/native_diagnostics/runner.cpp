// Diagnostic only. Reuse the exact production pipeline, never its IPC entrypoint.
// This frontend does not change production selection, GUI or release targets.
#define main unused_production_worker_main
#include "../../native/ocr/worker.cpp"
#undef main
#include <dlfcn.h>
#include <sys/resource.h>
#include <unordered_map>
#include "metal_capture.hpp"

namespace diagnostic {
using Clock = std::chrono::steady_clock;
double seconds(Clock::time_point begin) {
  return std::chrono::duration<double>(Clock::now() - begin).count();
}
const OrtApi *original = nullptr;
OrtApi wrapped{};
std::string mode;
fs::path output_dir;
bool evidence = false, capture = false;
size_t trace_sequence = 0, trace_bytes = 0;
double run_seconds = 0, create_seconds = 0;
J sessions = J::array(), traces = J::array(), errors = J::array();
std::unordered_map<const OrtSession *, size_t> session_index;
J execution_clock() {
  return {{"utc_unix_ns", std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count()},
          {"steady_clock_ns", std::chrono::duration_cast<std::chrono::nanoseconds>(
      Clock::now().time_since_epoch()).count()}};
}

J providers() {
  char **names = nullptr;
  int count = 0;
  check(original->GetAvailableProviders(&names, &count));
  J values = J::array();
  for (int i = 0; i < count; ++i) values.push_back(names[i]);
  check(original->ReleaseAvailableProviders(names, count));
  return values;
}
void save_json(const fs::path &path, const J &value) {
  std::ofstream stream(path);
  stream << value.dump(2) << '\n';
  if (!stream) throw std::runtime_error("cannot write diagnostic JSON");
}
J tensor_file(const OrtValue *value, const std::string &name) {
  Info info;
  check(original->GetTensorTypeAndShape(value, &info.p));
  ONNXTensorElementDataType dtype;
  check(original->GetTensorElementType(info.p, &dtype));
  if (dtype != ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT)
    throw std::runtime_error("diagnostic requires FP32 tensors");
  size_t rank = 0, elements = 0;
  check(original->GetDimensionsCount(info.p, &rank));
  std::vector<int64_t> shape(rank);
  check(original->GetDimensions(info.p, shape.data(), rank));
  check(original->GetTensorShapeElementCount(info.p, &elements));
  if (elements > 64 * 1024 * 1024 || trace_bytes + elements * 4 > 256 * 1024 * 1024)
    throw std::runtime_error("diagnostic tensor capture exceeds 256 MiB");
  trace_bytes += elements * 4;
  void *data = nullptr;
  check(original->GetTensorMutableData(const_cast<OrtValue *>(value), &data));
  std::ofstream stream(output_dir / name, std::ios::binary);
  stream.write(static_cast<const char *>(data), elements * 4);
  if (!stream) throw std::runtime_error("cannot save FP32 tensor");
  return {{"file", name}, {"shape", shape}, {"dtype", "<f4"}};
}
OrtStatus *ORT_API_CALL create_session(const OrtEnv *env, const ORTCHAR_T *path,
                                     const OrtSessionOptions *options,
                                     OrtSession **session) noexcept {
  OrtSessionOptions *copy = nullptr;
  try {
    check(original->CloneSessionOptions(options, &copy));
    const size_t index = sessions.size();
    const auto prefix = output_dir / ("ort-profile-" + std::to_string(index));
    if (evidence) {
      check(original->EnableProfiling(copy, prefix.c_str()));
      check(original->SetSessionLogSeverityLevel(copy, ORT_LOGGING_LEVEL_VERBOSE));
      check(original->SetSessionLogVerbosityLevel(copy, 1));
    }
    if (mode != "ort-cpu") {
      const char *keys[] = {"ModelFormat", "MLComputeUnits", "RequireStaticInputShapes",
                           "EnableOnSubgraphs", "AllowLowPrecisionAccumulationOnGPU",
                           "ProfileComputePlan", "ModelCacheDirectory"};
      const std::string cache = evidence ? (output_dir / ("coreml-cache-" + std::to_string(index))).string() : "";
      const char *values[] = {"MLProgram", mode == "coreml-cpu" ? "CPUOnly" : "CPUAndGPU",
                             "0", "0", "0", evidence ? "1" : "0", cache.c_str()};
      check(original->SessionOptionsAppendExecutionProvider(copy, "CoreML", keys, values, 7));
    }
    const auto started = Clock::now();
    auto *status = original->CreateSession(env, path, copy, session);
    const double elapsed = seconds(started);
    create_seconds += elapsed;
    original->ReleaseSessionOptions(copy);
    copy = nullptr;
    if (status) return status; // Never silently substitute CPU after provider failure.
    sessions.push_back({{"model", fs::path(path).filename().string()},
                        {"creation_seconds", elapsed}, {"profile", nullptr}});
    session_index[*session] = index;
    return nullptr;
  } catch (const std::exception &error) {
    if (copy) original->ReleaseSessionOptions(copy);
    return original->CreateStatus(ORT_FAIL, error.what());
  }
}
OrtStatus *ORT_API_CALL run(OrtSession *session, const OrtRunOptions *options,
                          const char *const *input_names, const OrtValue *const *inputs,
                          size_t input_count, const char *const *output_names,
                          size_t output_count, OrtValue **outputs) noexcept {
  const auto run_begin = capture ? execution_clock() : J(nullptr);
  const auto started = Clock::now();
  auto *status = original->Run(session, options, input_names, inputs, input_count,
                               output_names, output_count, outputs);
  run_seconds += seconds(started);
  const auto run_end = capture ? execution_clock() : J(nullptr);
  if (status || !capture) return status;
  try {
    if (input_count != 1 || output_count != 1)
      throw std::runtime_error("only one-input/one-output OCR models are supported");
    const auto stem = "tensor-" + std::to_string(trace_sequence++);
    traces.push_back({{"model", sessions.at(session_index.at(session)).at("model")},
                      {"execution_window", {{"worker_pid", getpid()}, {"begin", run_begin},
                                             {"end", run_end}, {"scope", "synchronous OrtRun"}}},
                      {"input", tensor_file(inputs[0], stem + "-input.f32")},
                      {"output", tensor_file(outputs[0], stem + "-output.f32")}});
    return nullptr;
  } catch (const std::exception &error) {
    return original->CreateStatus(ORT_FAIL, error.what());
  }
}
void ORT_API_CALL release_session(OrtSession *session) noexcept {
  if (evidence && session) {
    OrtAllocator *allocator = nullptr;
    char *profile = nullptr;
    try {
      check(original->GetAllocatorWithDefaultOptions(&allocator));
      check(original->SessionEndProfiling(session, allocator, &profile));
      sessions.at(session_index.at(session))["profile"] = fs::path(profile).filename().string();
      allocator->Free(allocator, profile);
    } catch (const std::exception &error) {
      errors.push_back(error.what());
    }
  }
  session_index.erase(session);
  original->ReleaseSession(session);
}
J runtime_info() {
  Dl_info loaded{};
  dladdr(reinterpret_cast<const void *>(&OrtGetApiBase), &loaded);
  return {{"ort_version", OrtGetApiBase()->GetVersionString()},
          {"ort_build", original->GetBuildInfoString()}, {"available_providers", providers()},
          {"loaded_ort_library", loaded.dli_fname ? loaded.dli_fname : ""},
          {"opencv_version", CV_VERSION}, {"opencv_build", cv::getBuildInformation()}};
}
Tensor load_tensor(const J &record) {
  if (record.at("dtype") != "<f4") throw std::runtime_error("FP32 input required");
  Tensor value;
  value.shape = record.at("shape").get<std::vector<int64_t>>();
  size_t count = 1;
  if (value.shape.empty() || value.shape.size() > 8) throw std::runtime_error("invalid tensor rank");
  for (auto n : value.shape) {
    if (n <= 0 || n > 16000000 || count > 16000000 / size_t(n))
      throw std::runtime_error("invalid/bounded tensor shape");
    count *= n;
  }
  const fs::path path(record.at("path").get<std::string>());
  if (fs::file_size(path) != count * 4) throw std::runtime_error("tensor byte count mismatch");
  value.data.resize(count);
  std::ifstream stream(path, std::ios::binary);
  stream.read(reinterpret_cast<char *>(value.data.data()), count * 4);
  if (!stream) throw std::runtime_error("cannot read tensor");
  return value;
}
} // namespace diagnostic

int main(int argc, char **argv) {
  using namespace diagnostic;
  const auto entry_started = Clock::now();
  try {
    // API-level opt-out is too late for the initialization event. The launcher
    // must set this in the child's environment BEFORE loading this executable.
    const char *telemetry = std::getenv("ORT_DISABLE_TELEMETRY");
    if (!telemetry || std::string(telemetry) != "1")
      throw std::runtime_error("launch requires ORT_DISABLE_TELEMETRY=1 before process initialization");
    original = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!original || std::string(OrtGetApiBase()->GetVersionString()) != "1.31.0")
      throw std::runtime_error("diagnostic requires the pinned ORT 1.31.0 runtime");
    api = original;
    if (argc == 2 && (std::string(argv[1]) == "--probe" ||
                      std::string(argv[1]) == "--probe-metal")) {
      auto info = runtime_info();
      if (std::string(argv[1]) == "--probe-metal")
        info["metal_capture"] = diagnostic_capture::capabilities();
      std::cout << info.dump(2) << '\n';
      return 0;
    }
    if (argc != 3) throw std::runtime_error("usage: diagnostic REQUEST.json NEW_OUTPUT_DIR | --probe");
    J request;
    std::ifstream(argv[1]) >> request;
    if (request.at("schema_version") != 1) throw std::runtime_error("unknown request version");
    mode = request.at("mode");
    if (mode != "ort-cpu" && mode != "coreml-cpu" && mode != "coreml-gpu-permitted")
      throw std::runtime_error("invalid diagnostic mode");
    if (mode != "ort-cpu") {
      bool coreml = false;
      for (const auto &provider : providers()) coreml |= provider == "CoreMLExecutionProvider";
      if (!coreml) throw std::runtime_error("CoreMLExecutionProvider unavailable; CPU substitution refused");
    }
    const uint32_t endian = 1;
    if (*reinterpret_cast<const unsigned char *>(&endian) != 1 || sizeof(float) != 4)
      throw std::runtime_error("little-endian FP32 host required");
    output_dir = fs::absolute(argv[2]);
    if (!fs::create_directory(output_dir)) throw std::runtime_error("output must not exist");
    evidence = request.value("evidence", false);
    const bool metal_capture = request.value("metal_capture", false);
    if (metal_capture && (!evidence || request.value("warmup", 0) != 0 ||
                         request.value("repeats", 0) != 0))
      throw std::runtime_error("Metal capture is single-pass evidence only, never timed/warmup samples");
    struct CaptureGuard { ~CaptureGuard() { diagnostic_capture::stop(); } } capture_guard;
    wrapped = *original;
    wrapped.CreateSession = create_session;
    wrapped.Run = run;
    wrapped.ReleaseSession = release_session;
    api = &wrapped;
    const int threads = bounded_int(request, "threads", 4, 1, 128);
    const int warmups = bounded_int(request, "warmup", 3, 0, 1000);
    const int repeats = bounded_int(request, "repeats", 10, 0, 10000);
    const int timeout = bounded_int(request, "timeout_ms", 120000, 1, 120000);
    const int batch = bounded_int(request, "batch_size", 6, 1, 100);
    const std::string kind = request.at("kind");
    if (kind != "pipeline" && kind != "tensor") throw std::runtime_error("unsupported kind");
    std::vector<cv::Mat> images;
    std::vector<Tensor> tensors;
    const auto input_started = Clock::now();
    if (kind == "pipeline") {
      for (const auto &path : request.at("images")) images.push_back(read_image(path));
    } else {
      for (const auto &record : request.at("inputs")) tensors.push_back(load_tensor(record));
    }
    if (images.empty() && tensors.empty()) throw std::runtime_error("empty input corpus");
    const double input_load = seconds(input_started);
    OrtEnv *environment = nullptr;
    check(api->CreateEnv(evidence ? ORT_LOGGING_LEVEL_VERBOSE : ORT_LOGGING_LEVEL_WARNING,
                         "vse-provider-diagnostic", &environment));
    auto env_guard = std::unique_ptr<OrtEnv, void (*)(OrtEnv *)>(environment, [](OrtEnv *env) { api->ReleaseEnv(env); });
    check(api->DisableTelemetryEvents(environment));
    J report = runtime_info(), first_result, last_result, samples = J::array(), cold;
    report.update({{"schema_version", 1}, {"mode", mode}, {"kind", kind},
                   {"evidence_run", evidence}, {"gpu_execution_verified", false},
                   {"ane_allowed", false}, {"input_load_seconds", input_load},
                   {"warmup_passes", warmups}, {"measured_passes", repeats},
                   {"threads", threads}, {"batch_size", batch},
                   {"timing_scope", "synchronous outputs materialized; profiling/capture runs are evidence only"}});
    {
      std::unique_ptr<Engine> engine;
      std::unique_ptr<Session> session;
      if (kind == "pipeline") {
        const std::string tier = request.at("tier"), rec = request.at("rec_model");
        if ((tier != "small" && tier != "medium") || !model_name_valid(rec))
          throw std::runtime_error("invalid pipeline configuration");
        engine = std::make_unique<Engine>(environment, request.at("models").get<std::string>(),
                   request.at("config").get<std::string>(), tier, rec, threads, false);
      } else {
        session = std::make_unique<Session>(environment, request.at("model").get<std::string>(), threads);
      }
      report["initial_session_seconds"] = create_seconds;
      bool stable = true;
      for (int pass = -1 - warmups; pass < repeats; ++pass) {
        // One cold pass, N additional discarded warmups, then N measured passes.
        capture = evidence && pass == -1 - warmups;
        if (metal_capture && capture)
          report["metal_capture"] = diagnostic_capture::start(output_dir / "metal-capture.gputrace");
        J result = J::array();
        const double before_run = run_seconds, before_create = create_seconds;
        const auto started = Clock::now();
        if (engine) {
          for (const auto &image : images) result.push_back(engine->predict(image, timeout, false, batch));
        } else {
          for (auto &input : tensors) {
            auto output = session->run(input, timeout);
            // Numerical outputs are captured only in the separate evidence pass.
            result.push_back({{"shape", output.shape}});
          }
        }
        if (metal_capture && capture) {
          diagnostic_capture::stop();
          report["metal_capture"]["stopped"] = true;
          report["metal_capture"]["worker_pid"] = getpid();
        }
        J timing = {{"wall_seconds", seconds(started)},
                    {"ort_run_seconds", run_seconds - before_run},
                    {"lazy_session_seconds", create_seconds - before_create}};
        if (pass == -1 - warmups) {
          cold = timing;
          first_result = result;
          report["process_entry_to_first_prediction_seconds"] = seconds(entry_started);
        }
        else if (engine && result != first_result) stable = false;
        if (pass >= 0) samples.push_back(timing);
        last_result = std::move(result);
      }
      report["pipeline_repeat_exactly_stable"] = engine ? J(stable) : J(nullptr);
    } // End sessions/profiles before serializing their evidence.
    struct rusage usage{};
    getrusage(RUSAGE_SELF, &usage);
#ifdef __APPLE__
    report["max_rss_bytes"] = usage.ru_maxrss;
#else
    report["max_rss_bytes"] = usage.ru_maxrss * 1024LL;
#endif
    report.update({{"cold_first_pass", cold}, {"samples", samples}, {"sessions", sessions},
                   {"traces", traces}, {"profile_errors", errors},
                   {"first_result", first_result}, {"last_result", last_result},
                   {"video_end_to_end_seconds", nullptr},
                   {"video_scope", "OCR/tensor replay only; not a production video run"}});
    save_json(output_dir / "result.json", report);
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "diagnostic failed (no provider fallback): " << error.what() << '\n';
    return 2;
  }
}
