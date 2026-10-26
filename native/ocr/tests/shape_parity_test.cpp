// Opt-in offline CPU control: actual pinned dynamic model vs same model bytes
// specialized only through official session options. Never a CoreML/GPU claim.
#define main unused_worker_main
#define wmain unused_worker_wmain
#include "../worker.cpp"
#undef main
#undef wmain
int main(int argc, char **argv) {
  try {
    const char *optout = std::getenv("ORT_DISABLE_TELEMETRY");
    if (!optout || std::string(optout) != "1")
      throw std::runtime_error("ORT_DISABLE_TELEMETRY=1 is required before exec");
    if (argc != 2) throw std::runtime_error("expected verified model directory");
    native_protocol::Output protocol;
    api = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!api || std::string(OrtGetApiBase()->GetVersionString()) != "1.31.0")
      throw std::runtime_error("pinned ORT 1.31.0 required");
    OrtEnv *environment = nullptr;
    check(api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "shape-parity", &environment));
    check(api->DisableTelemetryEvents(environment));
    J cases = J::array();
    try {
      for (const auto &file : fs::directory_iterator(argv[1])) {
        if (file.path().extension() != ".onnx") continue;
        bool detector = file.path().filename().string().find("_det_") != std::string::npos;
        const std::vector<std::vector<int64_t>> shapes = detector
          ? std::vector<std::vector<int64_t>>{{1,3,96,160}, {1,3,160,96}, {1,3,64,320}}
          : std::vector<std::vector<int64_t>>{{2,3,48,320}, {1,3,48,333}, {3,3,48,384}};
        Session dynamic(environment, file.path(), 2);
        for (const auto &shape : shapes) {
          Session bound(environment, file.path(), 2, "cpu", shape);
          Tensor tensor; tensor.shape = shape;
          size_t elements = 1;
          for (auto dim : shape) elements *= dim;
          tensor.data.resize(elements);
          for (size_t i = 0; i < elements; ++i)
            tensor.data[i] = float(int((i * 17) % 31) - 15) / 31.f;
          auto expected = dynamic.run(tensor, 120000);
          auto actual = bound.run(tensor, 120000);
          if (actual.shape != expected.shape || actual.data.size() != expected.data.size())
            throw std::runtime_error("bound CPU output shape mismatch");
          double maximum = 0, maximum_relative = 0, expected_scale = 0, actual_scale = 0;
          for (size_t i = 0; i < actual.data.size(); ++i) {
            if (!std::isfinite(actual.data[i]) || !std::isfinite(expected.data[i]))
              throw std::runtime_error("non-finite shape parity output");
            const double difference = std::abs(double(actual.data[i]) - expected.data[i]);
            maximum = std::max(maximum, difference);
            maximum_relative = std::max(maximum_relative,
                difference / std::max(1e-30, std::abs(double(expected.data[i]))));
            expected_scale = std::max(expected_scale, std::abs(double(expected.data[i])));
            actual_scale = std::max(actual_scale, std::abs(double(actual.data[i])));
          }
          auto wrong = tensor;
          ++wrong.shape[0];
          bool rejected = false;
          try { bound.run(wrong, 120000); }
          catch (const std::exception &) { rejected = true; }
          if (!rejected) throw std::runtime_error("bound session accepted mismatched input shape");
          J decoded;
          if (detector) {
            auto expected_boxes = detect(expected, shape[3], shape[2], 1000);
            auto actual_boxes = detect(actual, shape[3], shape[2], 1000);
            decoded = {{"same_detected_boxes", expected_boxes == actual_boxes},
                       {"dynamic_box_count", expected_boxes.size()},
                       {"bound_box_count", actual_boxes.size()},
                       {"decode_scope", "synthetic tensor; native detector default postprocess"}};
          } else {
            auto decode = [](const Tensor &output) {
              if (output.shape.size() != 3) throw std::runtime_error("invalid recognizer output rank");
              J rows = J::array();
              for (int64_t b = 0; b < output.shape[0]; ++b) {
                std::vector<int> tokens;
                int previous = -1;
                float confidence = 0;
                for (int64_t t = 0; t < output.shape[1]; ++t) {
                  auto begin = output.data.begin() + (b * output.shape[1] + t) * output.shape[2];
                  int token = std::max_element(begin, begin + output.shape[2]) - begin;
                  if (token && token != previous) { tokens.push_back(token); confidence += begin[token]; }
                  previous = token;
                }
                rows.push_back({{"tokens", tokens},
                                {"confidence", tokens.empty() ? 0.f : confidence / tokens.size()}});
              }
              return rows;
            };
            const auto expected_rows = decode(expected), actual_rows = decode(actual);
            bool same = true;
            double confidence_delta = 0;
            for (size_t i = 0; i < expected_rows.size(); ++i) {
              same = same && expected_rows[i]["tokens"] == actual_rows[i]["tokens"];
              confidence_delta = std::max(confidence_delta, std::abs(
                  expected_rows[i]["confidence"].get<double>() - actual_rows[i]["confidence"].get<double>()));
            }
            decoded = {{"same_decoded_token_sequences", same},
                       {"max_decoded_confidence_delta", confidence_delta},
                       {"decode_scope", "synthetic tensor native CTC tokens; same pinned dictionary maps identical tokens to identical text"}};
          }
          cases.push_back({{"model", file.path().filename().string()}, {"shape", shape},
                           {"max_abs_delta", maximum}, {"passed_1e_6", maximum <= 1e-6},
                           {"max_relative_delta", maximum_relative},
                           {"relative_denominator_floor", 1e-30},
                           {"dynamic_max_abs_output", expected_scale},
                           {"bound_max_abs_output", actual_scale},
                           {"decoded", decoded},
                           {"mismatch_rejected", true}});
        }
      }
    } catch (...) { api->ReleaseEnv(environment); throw; }
    api->ReleaseEnv(environment);
    if (cases.size() != 45) throw std::runtime_error("expected all 15 pinned models / three shapes");
    bool passed = true;
    for (const auto &row : cases) passed = passed && row.at("passed_1e_6").get<bool>();
    protocol.capabilities(J({{"scope", "CPU shape-specialization control only"},
                            {"cases", cases}, {"gpu_execution_verified", false}}).dump());
    return passed ? 0 : 2;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}
