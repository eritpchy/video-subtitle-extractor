// Isolated evidence collection only; never linked into the production worker.
#pragma once
#include "json.hpp"
#include <filesystem>
namespace diagnostic_capture {
nlohmann::json capabilities();
nlohmann::json start(const std::filesystem::path &output);
void stop() noexcept;
}
