// Opt-in offline CPU control: actual pinned dynamic model vs same model bytes
// specialized only through official session options. Never a CoreML/GPU claim.
#define main unused_worker_main
#define wmain unused_worker_wmain
#include "../worker.cpp"
#undef main
#undef wmain
J cache_control(OrtEnv *environment, const fs::path &path, size_t capacity) {
  Session dispatcher(environment, path, 2, "cpu", {}, nullptr, capacity);
  dispatcher.shape_dispatcher = true;
  dispatcher.input_metadata = native_shape::read_input(path);
  const bool detector = path.filename().string().find("_det_") != std::string::npos;
  const int64_t height = detector ? 64 : 48;
  uint64_t first = 0, last = 0;
  for (size_t i = 0; i < capacity + 2; ++i) {
    const int64_t width = i == capacity + 1 ? 320 : 320 + 32 * i;
    Tensor tensor;
    tensor.shape = {1, 3, height, width};
    tensor.data.resize(3 * height * width, 0);
    dispatcher.run(tensor, 120000);
    last = dispatcher.shape_cache.back()->shape_id;
    if (!i) first = last;
    if (dispatcher.shape_cache.size() > capacity)
      throw std::runtime_error("exact shape LRU exceeded capacity");
  }
  if (first == last || dispatcher.request_shapes.size() != capacity + 2)
    throw std::runtime_error("evicted shape reused identity or lost current-request snapshot");
  const auto lifetime = dispatcher.run_count;
  dispatcher.begin_request();
  if (!dispatcher.request_shapes.empty() || dispatcher.request_run_count != 0)
    throw std::runtime_error("new request retained old shape evidence");
  Tensor tensor;
  tensor.shape = {1, 3, height, 320}; tensor.data.resize(3 * height * 320, 0);
  dispatcher.run(tensor, 120000);
  if (dispatcher.shape_cache.back()->shape_id != last ||
      dispatcher.request_shapes.size() != 1 || dispatcher.run_count != lifetime + 1)
    throw std::runtime_error("cached exact shape failed reuse/count invariant");
  return {{"model", path.filename().string()}, {"capacity", capacity},
          {"evicted_instance_snapshot_preserved", true}, {"evicted_id_not_reused", true},
          {"exact_shape_cache_hit", true}, {"next_request_evidence_reset", true},
          {"scope", "actual CPU dispatcher lifecycle control, not CoreML execution evidence"}};
}
J pipeline_control(OrtEnv *environment, const fs::path &models) {
  const fs::path tests = fs::path(__FILE__).parent_path();
  const auto configs = models.parent_path() / "config";
  std::ifstream stream(tests / "goldens/smoke.json");
  J goldens; stream >> goldens;
  const double tolerance = goldens.at("confidence_tolerance");
  J rows = J::array();
  for (const std::string tier : {"small", "medium"}) {
    Engine dynamic(environment, models, configs, tier, "PP-OCRv6_" + tier + "_rec", 2, false);
    Engine bound(environment, models, configs, tier, "PP-OCRv6_" + tier + "_rec", 2, false);
    // Test-only exercise of the same exact-shape dispatcher using CPU children.
    // Do NOT call provider_evidence(): these are CPU controls, not CoreML proof.
    bound.det.shape_dispatcher = true;
    bound.det.input_metadata = native_shape::read_input(bound.det.model_path);
    bound.rec = std::make_unique<Session>(environment, bound.rec_path, 2);
    bound.rec->shape_dispatcher = true;
    bound.rec->input_metadata = native_shape::read_input(bound.rec_path);
    for (const std::string fixture : {"english.png", "chinese.png", "multiline_2.5.png", "blank.png"}) {
      const auto image = read_image((tests / "evidence/fixtures" / fixture).string());
      auto golden = std::find_if(goldens["cases"].begin(), goldens["cases"].end(), [&](const J &value) {
        return value.at("tier") == tier && value.at("operation") == "ocr" &&
               value.at("fixture") == "fixtures/" + fixture;
      });
      if (golden == goldens["cases"].end()) throw std::runtime_error("frozen pipeline golden missing");
      for (int batch : {2, 3}) {
        auto expected = dynamic.predict(image, 120000, false, batch);
        auto actual = bound.predict(image, 120000, false, batch);
        auto compare = [&](const J &left, const J &right) {
          bool same_text = left[1].size() == right[1].size();
          double delta = 0;
          if (same_text) for (size_t i = 0; i < left[1].size(); ++i) {
            same_text = same_text && left[1][i][0] == right[1][i][0];
            delta = std::max(delta, std::abs(left[1][i][1].get<double>() - right[1][i][1].get<double>()));
          }
          const bool boxes = left[0] == right[0];
          return J{{"same_boxes", boxes}, {"same_text", same_text}, {"confidence_delta", delta},
                   {"passed", boxes && same_text && delta <= tolerance}};
        };
        auto parity = compare(actual, expected);
        auto frozen = compare(actual, golden->at("expected"));
        rows.push_back({{"tier", tier}, {"fixture", fixture}, {"batch_size", batch},
                        {"dynamic_vs_bound", parity}, {"bound_vs_frozen", frozen},
                        {"bound_result", actual},
                        {"det_cached_shapes", bound.det.shape_cache.size()},
                        {"rec_cached_shapes", bound.rec->shape_cache.size()},
                        {"passed", parity.at("passed").get<bool>() && frozen.at("passed").get<bool>()}});
      }
    }
  }
  return rows;
}
int main(int argc, char **argv) {
  try {
    const char *optout = std::getenv("ORT_DISABLE_TELEMETRY");
    if (!optout || std::string(optout) != "1")
      throw std::runtime_error("ORT_DISABLE_TELEMETRY=1 is required before exec");
    if (argc != 2 && !(argc == 3 && std::string(argv[2]) == "--pipeline"))
      throw std::runtime_error("expected verified model directory and optional --pipeline");
    native_protocol::Output protocol;
    api = OrtGetApiBase()->GetApi(ORT_API_VERSION);
    if (!api || std::string(OrtGetApiBase()->GetVersionString()) != "1.31.0")
      throw std::runtime_error("pinned ORT 1.31.0 required");
    OrtEnv *environment = nullptr;
    check(api->CreateEnv(ORT_LOGGING_LEVEL_WARNING, "shape-parity", &environment));
    check(api->DisableTelemetryEvents(environment));
    if (argc == 3) {
      J cases, cache;
      try {
        cases = pipeline_control(environment, argv[1]);
        cache = J::array({cache_control(environment, fs::path(argv[1]) / "PP-OCRv6_small_det_infer.onnx", 2),
                          cache_control(environment, fs::path(argv[1]) / "PP-OCRv6_small_rec_infer.onnx", 4)});
      }
      catch (...) { api->ReleaseEnv(environment); throw; }
      api->ReleaseEnv(environment);
      bool passed = true;
      for (const auto &row : cases) passed = passed && row.at("passed").get<bool>();
      protocol.capabilities(J({{"scope", "CPU-only exact-shape pipeline control; no CoreML EP evidence"},
                              {"cases", cases}, {"gpu_execution_verified", false},
                              {"cache_controls", cache},
                              {"passed", passed}}).dump());
      return passed ? 0 : 2;
    }
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
