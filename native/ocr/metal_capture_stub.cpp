#include "metal_capture.hpp"
#include <stdexcept>
namespace diagnostic_capture {
nlohmann::json capabilities() {
  return {{"available", false}, {"reason", "Metal capture requires macOS"},
          {"gpu_execution_verified", false}};
}
nlohmann::json start(const std::filesystem::path &) {
  throw std::runtime_error("Metal capture unavailable; no alternative profiler attempted");
}
void stop() noexcept {}
}
