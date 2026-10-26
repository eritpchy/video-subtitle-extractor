// Explicit diagnostic evidence only. Normal worker execution never calls this API.
#pragma once
#include "json.hpp"
#include <filesystem>
namespace diagnostic_capture {
nlohmann::json capabilities();
nlohmann::json start(const std::filesystem::path &output);
void stop() noexcept;
}
