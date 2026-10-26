// Provider execution evidence. Pure JSON logic, also exercised without ORT.
#pragma once
#include "json.hpp"
#include <set>
#include <stdexcept>
#include <string>

namespace native_provider {
using Json = nlohmann::json;

inline Json coreml_configuration(bool available) {
  return {{"available", available}, {"model_format", "MLProgram"},
          {"compute_units", "CPUAndGPU"}, {"ane_allowed", false},
          {"gpu_execution_verified", false},
          {"scope", "CoreML CPU/GPU permission, not proof of actual GPU dispatch"}};
}

inline Json initial_session_evidence(const std::string &provider,
                                     const std::string &model) {
  return {{"model", model}, {"status", "not_run"},
          {"source", provider == "coreml" ? "ort_first_run_profile"
                                           : "explicit_cpu_session"},
          {"coreml_ep_executed", false}, {"cpu_ep_executed", false},
          {"coreml_kernel_events", 0}, {"cpu_kernel_events", 0},
          {"observed_providers", Json::array()}, {"gpu_execution_verified", false},
          {"audit_scope", provider == "coreml"
              ? "summary_only when executed; production raw profile is deleted after validation; use the independent diagnostic to retain raw profiles"
              : "explicit CPU configuration; profiling is disabled"},
          {"scope", "ORT EP execution only; CoreML internal CPU/GPU dispatch and "
                    "internal fallback are not measured. Event counts are not "
                    "operator coverage or GPU utilization."}};
}

inline Json summarize_coreml_profile(const Json &events,
                                    const std::string &model) {
  if (!events.is_array() || events.size() > 200000)
    throw std::runtime_error("invalid or oversized CoreML execution profile");
  Json result = initial_session_evidence("coreml", model);
  size_t coreml = 0, cpu = 0;
  std::set<std::string> providers;
  for (const auto &event : events) {
    if (!event.is_object() || event.value("cat", std::string()) != "Node")
      continue;
    const auto event_name = event.value("name", std::string());
    const std::string kernel_suffix = "_kernel_time";
    if (event_name.size() < kernel_suffix.size() ||
        event_name.compare(event_name.size() - kernel_suffix.size(),
                           kernel_suffix.size(), kernel_suffix) != 0)
      continue;
    auto arguments = event.find("args");
    if (arguments == event.end() || !arguments->is_object())
      continue;
    auto provider = arguments->find("provider");
    if (provider == arguments->end())
      continue;
    if (!provider->is_string())
      throw std::runtime_error("invalid provider in CoreML execution profile");
    const auto name = provider->get<std::string>();
    if (name.size() > 128)
      throw std::runtime_error("oversized provider in CoreML execution profile");
    providers.insert(name);
    if (name == "CoreMLExecutionProvider")
      ++coreml;
    else if (name == "CPUExecutionProvider")
      ++cpu;
    else
      throw std::runtime_error("unexpected execution provider in CoreML profile");
  }
  if (!coreml)
    throw std::runtime_error(
        "CoreML request produced no observed CoreML kernel execution; "
        "all-CPU fallback is refused (select CPU explicitly to use CPU)");
  result["status"] = "executed";
  result["coreml_ep_executed"] = true;
  result["cpu_ep_executed"] = cpu > 0;
  result["coreml_kernel_events"] = coreml;
  result["cpu_kernel_events"] = cpu;
  result["observed_providers"] = providers;
  return result;
}
} // namespace native_provider
