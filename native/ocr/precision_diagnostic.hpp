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
inline void geometry(const std::array<cv::Point2f, 4> &det_quad,
                     const std::array<cv::Point2f, 4> &crop_quad,
                     const cv::Mat &perspective, int warp_width, int warp_height,
                     bool rotated, const cv::Mat &pre_resize) {
  static size_t sequence = 0, total_bytes = 0;
  const char *value = std::getenv("VSE_OCR_DIAGNOSTIC_TRACE_DIR");
  if (!value || !*value || pre_resize.type() != CV_8UC3 ||
      perspective.type() != CV_64F || perspective.rows != 3 || perspective.cols != 3)
    throw std::runtime_error("invalid diagnostic geometry or trace directory");
  const size_t bytes = pre_resize.total() * pre_resize.elemSize();
  total_bytes += bytes;
  if (total_bytes > 256 * 1024 * 1024)
    throw std::runtime_error("diagnostic crop trace exceeds 256 MiB per process");
  fs::path root = fs::u8path(value);
  fs::create_directories(root);
  const auto stem = "geometry-" + std::to_string(sequence++);
  const auto filename = stem + "-pre-resize.u8";
  if (fs::exists(root / filename) || fs::exists(root / (stem + ".json")))
    throw std::runtime_error("refusing to overwrite diagnostic crop");
  auto contiguous = pre_resize.isContinuous() ? pre_resize : pre_resize.clone();
  std::ofstream pixels(root / filename, std::ios::binary);
  pixels.write(reinterpret_cast<const char *>(contiguous.data), bytes);
  if (!pixels)
    throw std::runtime_error("cannot save diagnostic crop pixels");
  J detected = J::array(), rectangle = J::array(), matrix = J::array();
  for (size_t i = 0; i < 4; ++i) {
    if (det_quad[i].x != std::nearbyint(det_quad[i].x) ||
        det_quad[i].y != std::nearbyint(det_quad[i].y))
      throw std::runtime_error("diagnostic detector quad is not integer-valued");
    detected.push_back({int(det_quad[i].x), int(det_quad[i].y)});
    rectangle.push_back({crop_quad[i].x, crop_quad[i].y});
  }
  for (int row = 0; row < 3; ++row)
    matrix.push_back({perspective.at<double>(row, 0), perspective.at<double>(row, 1),
                      perspective.at<double>(row, 2)});
  J record{{"schema_version", 1}, {"det_quad", detected},
           {"crop_min_area_rect", rectangle}, {"warp_perspective_matrix", matrix},
           {"warp_size", {warp_height, warp_width}}, {"rotated_90_counterclockwise", rotated},
           {"pre_resize", {{"file", filename}, {"dtype", "uint8"}, {"channel_order", "BGR"},
                           {"shape", {pre_resize.rows, pre_resize.cols, pre_resize.channels()}}}}};
  std::ofstream metadata(root / (stem + ".json"));
  metadata << record.dump(2) << '\n';
  if (!metadata)
    throw std::runtime_error("cannot save diagnostic geometry metadata");
}
} // namespace precision_diagnostic
