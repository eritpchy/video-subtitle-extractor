// Build-side only. Included exclusively by the opt-in, never-packaged target.
#pragma once
namespace precision_diagnostic {
inline std::string mode() {
  const char *value = std::getenv("VSE_OCR_DIAGNOSTIC_MODE");
  return value ? value : "baseline";
}
inline void configure(OrtSessionOptions *options) {
  auto value = mode();
  if (value != "baseline" && value != "bf16-off" && value != "extended" &&
      value != "basic" && value != "disabled" && value != "opencv-unoptimized")
    throw std::runtime_error("invalid precision diagnostic mode");
  if (value != "baseline")
    check(api->AddSessionConfigEntry(
        options, "mlas.enable_gemm_fastmath_arm64_bfloat16", "0"));
  if (value == "extended")
    check(api->SetSessionGraphOptimizationLevel(options, ORT_ENABLE_EXTENDED));
  if (value == "basic")
    check(api->SetSessionGraphOptimizationLevel(options, ORT_ENABLE_BASIC));
  if (value == "disabled")
    check(api->SetSessionGraphOptimizationLevel(options, ORT_DISABLE_ALL));
  if (value == "opencv-unoptimized")
    cv::setUseOptimized(false);
}
inline void trace(const fs::path &model, const Tensor &input, const Tensor &output) {
  static size_t sequence = 0, total_bytes = 0;
  const char *value = std::getenv("VSE_OCR_DIAGNOSTIC_TRACE_DIR");
  if (!value || !*value)
    throw std::runtime_error("diagnostic target requires a trace directory");
  const uint32_t endian = 1;
  if (*reinterpret_cast<const unsigned char *>(&endian) != 1 || sizeof(float) != 4)
    throw std::runtime_error("diagnostic requires little-endian FP32");
  total_bytes += (input.data.size() + output.data.size()) * sizeof(float);
  if (total_bytes > 256 * 1024 * 1024)
    throw std::runtime_error("diagnostic trace exceeds 256 MiB per process");
  fs::path root = fs::u8path(value);
  fs::create_directories(root);
  std::string stem = std::to_string(sequence++);
  auto save = [&](const Tensor &tensor, const char *kind) {
    auto name = stem + "-" + kind + ".f32";
    auto path = root / name;
    if (fs::exists(path))
      throw std::runtime_error("refusing to overwrite diagnostic tensor");
    std::ofstream file(path, std::ios::binary);
    file.write(reinterpret_cast<const char *>(tensor.data.data()),
               tensor.data.size() * sizeof(float));
    if (!file)
      throw std::runtime_error("cannot save diagnostic tensor");
    return J{{"file", name}, {"shape", tensor.shape}, {"dtype", "<f4"}};
  };
  J record{{"schema_version", 1}, {"mode", mode()},
           {"model", model.filename().u8string()},
           {"ort_version", OrtGetApiBase()->GetVersionString()},
           {"ort_build", api->GetBuildInfoString()},
           {"opencv_build", cv::getBuildInformation()},
           {"opencv_optimized", cv::useOptimized()},
           {"input", save(input, "input")}, {"output", save(output, "output")}};
  std::ofstream metadata(root / (stem + ".json"));
  metadata << record.dump(2) << '\n';
  if (!metadata)
    throw std::runtime_error("cannot save diagnostic metadata");
}
} // namespace precision_diagnostic
