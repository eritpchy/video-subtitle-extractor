// Apple's public, application-local Metal capture API. No debugger attachment,
// Instruments cache access, system permission changes, or external process trace.
#include "metal_capture.hpp"
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <cstdlib>
#include <stdexcept>
#include <string>

namespace diagnostic_capture {
namespace {
id<MTLDevice> device = nil;
bool active = false;
void require_enabled() {
  const char *value = std::getenv("MTL_CAPTURE_ENABLED");
  if (!value || std::string(value) != "1")
    throw std::runtime_error("Metal evidence requires MTL_CAPTURE_ENABLED=1 before exec");
}
}
nlohmann::json capabilities() {
  require_enabled();
  @autoreleasepool {
    if (!device) device = MTLCreateSystemDefaultDevice();
    const bool available = device && [[MTLCaptureManager sharedCaptureManager]
        supportsDestination:MTLCaptureDestinationGPUTraceDocument];
    return {{"available", available},
            {"device_name", device ? std::string([device.name UTF8String]) : ""},
            {"device_registry_id", device ? device.registryID : 0},
            {"device_source", "MTLCreateSystemDefaultDevice"},
            {"coreml_device_object_match_verified", false},
            {"capture_scope", "commands on this process's selected device object only"},
            {"gpu_execution_verified", false}};
  }
}
nlohmann::json start(const std::filesystem::path &output) {
  auto result = capabilities();
  if (!result.at("available").get<bool>())
    throw std::runtime_error("Metal GPUTraceDocument capture unavailable; no fallback");
  if (active || output.extension() != ".gputrace" ||
      std::filesystem::exists(output) || std::filesystem::is_symlink(output))
    throw std::runtime_error("Metal capture requires a new .gputrace in the evidence directory");
  @autoreleasepool {
    auto manager = [MTLCaptureManager sharedCaptureManager];
    if (manager.isCapturing)
      throw std::runtime_error("another Metal capture is active; refusing interference");
    auto descriptor = [[MTLCaptureDescriptor alloc] init];
    descriptor.captureObject = device;
    descriptor.destination = MTLCaptureDestinationGPUTraceDocument;
    descriptor.outputURL = [NSURL fileURLWithPath:[NSString stringWithUTF8String:output.c_str()]];
    NSError *error = nil;
    if (![manager startCaptureWithDescriptor:descriptor error:&error])
      throw std::runtime_error(std::string("Metal capture refused: ") +
          (error ? [error.localizedDescription UTF8String] : "unknown error"));
    active = true;
  }
  result["output"] = output.filename().string();
  result["started"] = true;
  result["timing_eligible"] = false;
  result["coverage"] = "unverified; trace creation is not proof of executed CoreML GPU dispatch";
  return result;
}
void stop() noexcept {
  if (active) {
    [[MTLCaptureManager sharedCaptureManager] stopCapture];
    active = false;
  }
}
}
