// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace vse {
inline std::wstring ascii_upper(std::wstring value) {
    for (auto& ch : value) if (ch >= L'a' && ch <= L'z') ch = static_cast<wchar_t>(ch - (L'a' - L'A'));
    return value;
}
inline bool component_ok(const std::wstring& value) {
    if (value.empty() || value.size() > 255 || value == L"." || value == L".." || value.back() == L'.' || value.back() == L' ') return false;
    for (wchar_t ch : value) if (ch < 32 || std::wstring(L"\\/:*?\"<>|").find(ch) != std::wstring::npos) return false;
    auto base = ascii_upper(value.substr(0, value.find(L'.')));
    // Win32 also treats spaces immediately preceding the extension as reserved.
    while (!base.empty() && base.back() == L' ') base.pop_back();
    if (base == L"CON" || base == L"PRN" || base == L"AUX" || base == L"NUL" || base == L"CONIN$" || base == L"CONOUT$") return false;
    if (base.size() == 4 && (base.substr(0, 3) == L"COM" || base.substr(0, 3) == L"LPT")) {
        const wchar_t last = base[3];
        if ((last >= L'0' && last <= L'9') || last == L'\u00b9' || last == L'\u00b2' || last == L'\u00b3') return false;
    }
    return true;
}
inline bool absolute_local_path_ok(const std::wstring& path) {
    if (path.size() < 3 || path.size() >= 32700 || !((path[0] >= L'A' && path[0] <= L'Z') || (path[0] >= L'a' && path[0] <= L'z')) || path[1] != L':' || path[2] != L'\\') return false;
    if (path.size() == 3) return true;
    std::size_t start = 3;
    while (start < path.size()) {
        const auto end = path.find(L'\\', start);
        if (!component_ok(path.substr(start, end == std::wstring::npos ? end : end - start))) return false;
        if (end == std::wstring::npos) return true;
        start = end + 1;
    }
    return false; // Reject a non-root trailing separator, not silently normalize it.
}
// Only use for OS-reported paths, never directly for a CLI/user input.
inline std::wstring ordinary_os_path(std::wstring path) {
    if (path.rfind(L"\\\\?\\", 0) == 0) path.erase(0, 4);
    if (!absolute_local_path_ok(path)) throw std::invalid_argument("OS path is not an ordinary local absolute path");
    return path;
}
inline std::wstring join(const std::wstring& parent, const std::wstring& leaf) {
    return parent + (parent.back() == L'\\' ? L"" : L"\\") + leaf;
}
// The Microsoft C argv grammar, not cmd.exe/PowerShell escaping.
inline std::wstring quote_arg(const std::wstring& value) {
    std::wstring result = L"\"";
    std::size_t slashes = 0;
    for (wchar_t ch : value) {
        if (ch == L'\\') { ++slashes; continue; }
        result.append(ch == L'\"' ? slashes * 2 + 1 : slashes, L'\\');
        result.push_back(ch);
        slashes = 0;
    }
    result.append(slashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}
inline std::uint64_t checked_add(std::uint64_t a, std::uint64_t b) {
    if (b > (std::numeric_limits<std::uint64_t>::max)() - a) throw std::overflow_error("size overflow");
    return a + b;
}
inline std::uint64_t required_space(std::uint64_t zip, std::uint64_t unpacked, std::uint64_t entries) {
    // A conservative allocation/metadata allowance, plus 64 MiB. Runtime write
    // errors still fail closed; preflight cannot reserve disk against others.
    if (entries > (std::numeric_limits<std::uint64_t>::max)() / 65536) throw std::overflow_error("entry overflow");
    return checked_add(checked_add(zip, unpacked), checked_add(entries * 65536, 64ULL * 1024 * 1024));
}
inline bool sha256_text_ok(const std::string& text) {
    if (text.size() != 64) return false;
    return std::all_of(text.begin(), text.end(), [](char ch) { return (ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f'); });
}
struct Part { const wchar_t* name; std::uint64_t bytes; const char* sha256; };
struct Entry {
    const wchar_t* name; std::uint64_t bytes; bool directory; const char* sha256;
    std::uint64_t data_offset; std::uint64_t compressed_bytes; unsigned method;
};
} // namespace vse
