// Minimal diagnostic: system C++/dlopen + exact ORT headers; no OpenCV/models.
#include "onnxruntime_c_api.h"
#include <cstdlib>
#include <dlfcn.h>
#include <iostream>
#include <stdexcept>
#include <string>

std::string quoted(const std::string &value) {
  std::string result = "\"";
  const char *hex = "0123456789abcdef";
  for (unsigned char c : value) {
    if (c == '"' || c == '\\') { result += '\\'; result += c; }
    else if (c < 32) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
    else result += c;
  }
  return result + '"';
}

int main(int argc, char **argv) {
  try {
    const char *disabled = std::getenv("ORT_DISABLE_TELEMETRY");
    if (!disabled || std::string(disabled) != "1")
      throw std::runtime_error("ORT_DISABLE_TELEMETRY=1 is required before exec/dlopen");
    if (argc != 2) throw std::runtime_error("usage: runtime-probe EXISTING_VERIFIED_ORT_LIBRARY");
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) throw std::runtime_error(dlerror());
    void *symbol = dlsym(library, "OrtGetApiBase");
    if (!symbol) throw std::runtime_error("OrtGetApiBase missing");
    using Entry = const OrtApiBase *(ORT_API_CALL *)() NO_EXCEPTION;
    const auto *base = reinterpret_cast<Entry>(symbol)();
    if (std::string(base->GetVersionString()) != "1.31.0")
      throw std::runtime_error("Only the pinned ORT 1.31.0 runtime is supported");
    const auto *api = base->GetApi(ORT_API_VERSION);
    if (!api) throw std::runtime_error("Pinned C API unavailable");
    auto check = [&](OrtStatus *status) {
      if (status) {
        std::string error = api->GetErrorMessage(status);
        api->ReleaseStatus(status);
        throw std::runtime_error(error);
      }
    };
    char **names = nullptr;
    int count = 0;
    check(api->GetAvailableProviders(&names, &count));
    Dl_info actual{};
    dladdr(symbol, &actual);
    std::cout << "{\"schema_version\":1,\"ort_version\":" << quoted(base->GetVersionString())
              << ",\"ort_build\":" << quoted(api->GetBuildInfoString())
              << ",\"loaded_ort_library\":" << quoted(actual.dli_fname ? actual.dli_fname : "")
              << ",\"available_providers\":[";
    for (int i = 0; i < count; ++i) {
      if (i) std::cout << ',';
      std::cout << quoted(names[i]);
    }
    check(api->ReleaseAvailableProviders(names, count));
    std::cout << "],\"inference_executed\":false,\"gpu_execution_verified\":false,"
                 "\"scope\":\"Actual runtime provider enumeration only; no application session, model or GPU execution\"}\n";
    dlclose(library);
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 2;
  }
}
