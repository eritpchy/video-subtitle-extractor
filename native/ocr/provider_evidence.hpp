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

inline Json summarize_shape_sessions(const std::string &model,
                                     const Json &snapshots,
                                     uint64_t lifetime_runs,
                                     uint64_t request_runs) {
  Json result = initial_session_evidence("coreml", model);
  uint64_t coreml = 0, cpu = 0, runs = 0;
  bool reused = false;
  std::set<std::string> providers;
  for (const auto &snapshot : snapshots) {
    if (snapshot.at("model") != model || snapshot.at("status") != "executed" ||
        !snapshot.at("coreml_ep_executed").get<bool>())
      throw std::runtime_error("invalid per-shape CoreML execution evidence");
    coreml += snapshot.at("coreml_kernel_events").get<uint64_t>();
    cpu += snapshot.at("cpu_kernel_events").get<uint64_t>();
    runs += snapshot.at("request_run_count").get<uint64_t>();
    reused = reused || snapshot.at("evidence_reused").get<bool>();
    for (const auto &provider : snapshot.at("observed_providers"))
      providers.insert(provider.get<std::string>());
  }
  if (runs != request_runs || request_runs > lifetime_runs ||
      ((request_runs > 0) != (coreml > 0)))
    throw std::runtime_error("per-shape CoreML run evidence mismatch");
  result["status"] = request_runs ? "executed" : "not_run";
  result["coreml_ep_executed"] = coreml > 0;
  result["cpu_ep_executed"] = cpu > 0;
  result["coreml_kernel_events"] = coreml;
  result["cpu_kernel_events"] = cpu;
  result["observed_providers"] = providers;
  result["session_run_count"] = lifetime_runs;
  result["request_run_count"] = request_runs;
  result["capture"] = "per_shape_first_successful_run";
  result["evidence_reused"] = reused;
  result["shape_sessions"] = snapshots;
  result["aggregation_scope"] = "Unique shape-session instances used by this request, including evicted instances; event counts sum their first-run profiles once, not per current Run. Lifetime runs include evicted sessions.";
  return result;
}
} // namespace native_provider
