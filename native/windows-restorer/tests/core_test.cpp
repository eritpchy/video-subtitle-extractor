// SPDX-License-Identifier: Apache-2.0
#include "../core.hpp"
#include <cassert>
#include <iostream>
int main() {
    for (auto value : {L"C:\\", L"C:\\a b &!%()", L"D:\\中文\\包", L"C:\\safe.name"}) assert(vse::absolute_local_path_ok(value));
    for (auto value : {L"a", L"C:foo", L"\\\\host\\share", L"\\\\?\\C:\\safe", L"\\\\.\\C:", L"C:/test", L"C:\\x\\", L"C:\\\\x", L"C:\\..\\x", L"C:\\a.", L"C:\\a ", L"C:\\NUL.txt", L"C:\\COM1", L"C:\\LPT\u00b9", L"C:\\a:b", L"C:\\CON .txt"}) assert(!vse::absolute_local_path_ok(value));
    assert(vse::absolute_local_path_ok(L"C:\\" + std::wstring(200, L'a') + L"\\" + std::wstring(200, L'b')));
    assert(!vse::component_ok(std::wstring(256, L'a')));
    assert(vse::ordinary_os_path(L"\\\\?\\C:\\a") == L"C:\\a");
    bool bad_os = false; try { vse::ordinary_os_path(L"\\\\?\\UNC\\host\\share"); } catch (const std::invalid_argument&) { bad_os = true; } assert(bad_os);
    assert(vse::quote_arg(L"") == L"\"\"");
    assert(vse::quote_arg(L"a b &!%()") == L"\"a b &!%()\"");
    assert(vse::quote_arg(L"C:\\") == L"\"C:\\\\\"");
    assert(vse::quote_arg(L"a\"b") == L"\"a\\\"b\"");
    assert(vse::quote_arg(L"a\\\"b\\") == L"\"a\\\\\\\"b\\\\\"");
    assert(vse::required_space(3'000'000'000, 7'000'000'000, 50'000) == 13'343'908'864ULL);
    bool failed = false; try { vse::required_space(UINT64_MAX, 1, 1); } catch (const std::overflow_error&) { failed = true; } assert(failed);
    constexpr std::uint64_t cap = 1024ULL * 1024 * 1024 * 1024;
    assert(vse::fixed_archive_bounds_ok(cap, 99, 100, 100));
    assert(!vse::fixed_archive_bounds_ok(cap + 1, 99, 100, 100));
    assert(!vse::fixed_archive_bounds_ok(9223372036854775807ULL, 99, 100, 100));
    assert(!vse::fixed_archive_bounds_ok(0, 100, 100, 100));
    assert(!vse::fixed_archive_bounds_ok(0, UINT64_MAX, 100, 100));
    assert(!vse::fixed_archive_bounds_ok(0, 99, 100, 101));
    assert(!vse::fixed_archive_bounds_ok(0, 99, 100, 99));
    assert(vse::sha256_text_ok(std::string(64, 'a')));
    assert(!vse::sha256_text_ok(std::string(64, 'A')));
    std::cout << "portable lexical/quoting/64-bit tests passed\n";
}
