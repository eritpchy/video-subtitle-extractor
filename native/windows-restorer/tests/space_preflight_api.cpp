// SPDX-License-Identifier: Apache-2.0
// SYNTHETIC-NOT-FOR-DISTRIBUTION. A separate non-LTCG translation unit so the
// caller cannot fold the simulated availability into an unreachable extractor.
#ifndef VSE_SYNTHETIC_SPACE_PREFLIGHT
#error This API wrapper is restricted to the synthetic space-report diagnostic.
#endif
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdio>
#include <cwchar>
extern "C" BOOL WINAPI VseSyntheticGetDiskFreeSpaceExW(
        LPCWSTR directory, PULARGE_INTEGER available, PULARGE_INTEGER total, PULARGE_INTEGER free_bytes) {
    // Validate/query the real path first, without reserving/filling any space.
    if (!GetDiskFreeSpaceExW(directory, available, total, free_bytes)) return FALSE;
    std::fwprintf(stderr, L"SYNTHETIC-NOT-FOR-DISTRIBUTION space-api actual_available=%llu reported_available=0; NOT real disk exhaustion\n",
                 static_cast<unsigned long long>(available->QuadPart));
    available->QuadPart = 0;
    return TRUE;
}
