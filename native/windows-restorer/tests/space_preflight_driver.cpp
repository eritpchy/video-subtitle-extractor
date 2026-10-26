// SPDX-License-Identifier: Apache-2.0
// SYNTHETIC-NOT-FOR-DISTRIBUTION: only this test entry redirects the Win32 API.
#ifndef VSE_SYNTHETIC_SPACE_PREFLIGHT
#error This entry is restricted to the synthetic space-report diagnostic builder.
#endif
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
extern "C" BOOL WINAPI VseSyntheticGetDiskFreeSpaceExW(LPCWSTR, PULARGE_INTEGER, PULARGE_INTEGER, PULARGE_INTEGER);
// Windows declarations are already included. The production source is included
// unchanged; there is no runtime/CLI/environment switch in the production EXE.
#define GetDiskFreeSpaceExW VseSyntheticGetDiskFreeSpaceExW
#include "../restorer.cpp"
#undef GetDiskFreeSpaceExW
