// A private protocol channel, isolated from dependency/framework stdout writes.
// This is process-lifetime isolation: never restore fd 1 during teardown.
#pragma once
#include <algorithm>
#include <cerrno>
#include <cstdio>
#include <cstdint>
#include <stdexcept>
#include <string>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <fcntl.h>
#include <io.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace native_protocol {
class Output {
  int descriptor_ = -1;
  void close() noexcept {
    if (descriptor_ < 0) return;
#ifdef _WIN32
    _close(descriptor_);
#else
    ::close(descriptor_);
#endif
    descriptor_ = -1;
  }
  void write_all(const char *data, size_t bytes) {
    while (bytes) {
      const auto count = std::min(bytes, size_t(1024 * 1024));
#ifdef _WIN32
      const auto written = _write(descriptor_, data, static_cast<unsigned int>(count));
#else
      const auto written = ::write(descriptor_, data, count);
#endif
      if (written < 0 && errno == EINTR) continue;
      if (written <= 0) throw std::runtime_error("native protocol output write failed");
      data += written;
      bytes -= static_cast<size_t>(written);
    }
  }
public:
  static constexpr size_t max_frame_bytes = 16 * 1024 * 1024;
  Output() {
    try {
#ifdef _WIN32
      descriptor_ = _dup(_fileno(stdout));
      if (descriptor_ < 0) throw std::runtime_error("cannot reserve native protocol stdout");
      const auto handle = reinterpret_cast<HANDLE>(_get_osfhandle(descriptor_));
      if (_setmode(descriptor_, _O_BINARY) < 0 ||
          !SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0))
        throw std::runtime_error("cannot protect native protocol output handle");
      if (_dup2(_fileno(stderr), _fileno(stdout)) < 0 ||
          !SetStdHandle(STD_OUTPUT_HANDLE,
              reinterpret_cast<HANDLE>(_get_osfhandle(_fileno(stdout)))))
        throw std::runtime_error("cannot isolate native dependency stdout");
#else
      descriptor_ = fcntl(STDOUT_FILENO, F_DUPFD_CLOEXEC, 3);
      if (descriptor_ < 0) throw std::runtime_error("cannot reserve native protocol stdout");
      if (dup2(STDERR_FILENO, STDOUT_FILENO) < 0)
        throw std::runtime_error("cannot isolate native dependency stdout");
#endif
      // Any buffered stdio diagnostic goes to stderr after redirection. Do not
      // flush it to the original protocol pipe before redirecting fd 1.
      if (std::fflush(stdout) != 0)
        throw std::runtime_error("cannot flush redirected dependency stdout");
    } catch (...) {
      close();
      throw;
    }
  }
  ~Output() { close(); }
  Output(const Output &) = delete;
  Output &operator=(const Output &) = delete;
  void capabilities(const std::string &json) {
    if (json.empty() || json.size() >= 65536)
      throw std::runtime_error("native capability output exceeds bounded JSON contract");
    write_all(json.data(), json.size());
    write_all("\n", 1);
  }
  void frame(const std::string &json) {
    if (json.empty() || json.size() > max_frame_bytes)
      throw std::runtime_error("native response exceeds the 16 MiB protocol limit");
    const auto size = static_cast<uint32_t>(json.size());
    const unsigned char header[4] = {
        static_cast<unsigned char>(size), static_cast<unsigned char>(size >> 8),
        static_cast<unsigned char>(size >> 16), static_cast<unsigned char>(size >> 24)};
    write_all(reinterpret_cast<const char *>(header), sizeof(header));
    write_all(json.data(), json.size());
  }
#ifdef VSE_NATIVE_PROTOCOL_TEST
  int test_descriptor() const { return descriptor_; }
#endif
};
} // namespace native_protocol
