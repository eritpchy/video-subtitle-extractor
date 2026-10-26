#include "../provider_evidence.hpp"
#include <iostream>

using J = nlohmann::json;
void expect(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
void rejects(const J &profile) {
  bool failed = false;
  try {
    native_provider::summarize_coreml_profile(profile, "model");
  } catch (const std::exception &) {
    failed = true;
  }
  expect(failed, "profile incorrectly accepted as CoreML execution");
}
J event(const J &provider) {
  return {{"cat", "Node"}, {"name", "unit_kernel_time"},
          {"args", {{"provider", provider}}}};
}
int main() {
  try {
    auto configuration = native_provider::coreml_configuration(true);
    expect(configuration["compute_units"] == "CPUAndGPU", "GPU permission changed");
    expect(configuration["ane_allowed"] == false, "ANE must not be permitted");
    expect(configuration["gpu_execution_verified"] == false,
           "configuration must not certify GPU execution");
    auto initial = native_provider::initial_session_evidence("cpu", "model");
    expect(initial["status"] == "not_run", "unexecuted session claimed execution");
    expect(initial["observed_providers"].empty(), "unexecuted session claimed provider evidence");
    rejects(J::array());
    rejects(J::object());
    rejects(J::array({event("CPUExecutionProvider")}));
    rejects(J::array({event("CUDAExecutionProvider")}));
    rejects(J::array({event(1)}));
    rejects(J::array({{{"cat", "ComputePlan"}, {"preferredComputeDevice", "GPU"}}}));
    rejects(J::array({{{"cat", "Session"}, {"args", {{"provider", "CoreMLExecutionProvider"}}}}}));
    rejects(J::array({{{"cat", "Node"}, {"name", "graph_assignment"},
                       {"args", {{"provider", "CoreMLExecutionProvider"}}}}}));
    auto mixed = native_provider::summarize_coreml_profile(
        J::array({event("CoreMLExecutionProvider"), event("CPUExecutionProvider"),
                  event("CoreMLExecutionProvider")}), "recognizer");
    expect(mixed["model"] == "recognizer", "model attribution lost");
    expect(mixed["status"] == "executed", "execution not recorded");
    expect(mixed["coreml_ep_executed"] == true && mixed["cpu_ep_executed"] == true,
           "mixed CPU/CoreML execution not visible");
    expect(mixed["coreml_kernel_events"] == 2 && mixed["cpu_kernel_events"] == 1,
           "incorrect kernel event counts");
    expect(mixed["observed_providers"].size() == 2, "provider set missing");
    expect(mixed["gpu_execution_verified"] == false,
           "CoreML profile must not certify internal GPU dispatch");
    auto coreml = native_provider::summarize_coreml_profile(
        J::array({event("CoreMLExecutionProvider")}), "detector");
    expect(coreml["cpu_ep_executed"] == false && coreml["cpu_kernel_events"] == 0,
           "CPU fallback invented");
    mixed["request_run_count"] = 2;
    mixed["evidence_reused"] = false;
    mixed["shape_session_id"] = 1;
    auto later_shape = mixed;
    later_shape["request_run_count"] = 1;
    later_shape["evidence_reused"] = true;
    later_shape["shape_session_id"] = 2;
    auto aggregate = native_provider::summarize_shape_sessions(
        "recognizer", J::array({mixed, later_shape}), 9, 3);
    expect(aggregate["coreml_kernel_events"] == 4 && aggregate["cpu_kernel_events"] == 2,
           "shape profile counts were multiplied by request runs");
    expect(aggregate["shape_sessions"].size() == 2 && aggregate["evidence_reused"] == true,
           "per-shape evidence or reuse lost");
    expect(!aggregate.contains("profile_window"), "aggregate invented a single profile window");
    auto fresh = native_provider::summarize_shape_sessions(
        "recognizer", J::array({mixed}), 50, 2);
    expect(fresh["evidence_reused"] == false,
           "old evicted shapes were incorrectly counted as reused evidence");
    auto blank = native_provider::summarize_shape_sessions("recognizer", J::array(), 50, 0);
    expect(blank["status"] == "not_run" && blank["shape_sessions"].empty() &&
           blank["coreml_kernel_events"] == 0 && blank["evidence_reused"] == false,
           "blank request inherited an earlier shape profile");
    std::cout << "provider evidence unit tests passed\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
