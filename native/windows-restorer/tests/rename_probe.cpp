// SPDX-License-Identifier: Apache-2.0
// SYNTHETIC DIAGNOSTIC ONLY. Never distribute as a restoration application.
// One case per process: an old-buffer crash cannot suppress other observations.
// No shell, network, registry/policy changes, cleanup, or replacement of data.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
#include "../core.hpp"

namespace {
static_assert(sizeof(wchar_t) == 2, "Native Windows UTF-16 only");
static_assert(sizeof(void*) == 8, "Native Windows x64 diagnostic only");
struct Failure { std::string stage; DWORD error; };
[[noreturn]] void fail(const char* stage, DWORD error = ERROR_INVALID_DATA) { throw Failure{stage, error}; }
[[noreturn]] void winfail(const char* stage) { fail(stage, GetLastError()); }
class Handle {
    HANDLE value_ = INVALID_HANDLE_VALUE;
public:
    Handle() = default;
    explicit Handle(HANDLE value) : value_(value) {}
    ~Handle() { if (valid()) CloseHandle(value_); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value_(other.value_) { other.value_ = INVALID_HANDLE_VALUE; }
    HANDLE get() const { return value_; }
    bool valid() const { return value_ && value_ != INVALID_HANDLE_VALUE; }
};
std::wstring wide(const char* value) {
    std::wstring result;
    for (; *value; ++value) result.push_back(static_cast<unsigned char>(*value));
    return result;
}
std::string quoted(const std::wstring& value) {
    static const char hex[] = "0123456789abcdef";
    std::string result = "\"";
    for (wchar_t ch : value) {
        const auto unit = static_cast<unsigned>(ch);
        if (ch == L'\\' || ch == L'\"') { result.push_back('\\'); result.push_back(static_cast<char>(ch)); }
        else if (unit >= 32 && unit < 127) result.push_back(static_cast<char>(ch));
        else {
            result += "\\u";
            for (int shift = 12; shift >= 0; shift -= 4) result.push_back(hex[(unit >> shift) & 15]);
        }
    }
    return result + '"';
}
using Record = std::map<std::string, std::string>;
void text(Record& row, const char* key, const char* value) { row[key] = quoted(wide(value)); }
void boolean(Record& row, const char* key, bool value) { row[key] = value ? "true" : "false"; }
void number(Record& row, const char* key, std::uint64_t value) { row[key] = std::to_string(value); }
void emit(const Record& row) {
    std::string result = "{";
    bool first = true;
    for (const auto& field : row) {
        if (!first) result += ',';
        first = false;
        result += quoted(wide(field.first.c_str())) + ':' + field.second;
    }
    result += "}\n";
    if (std::fwrite(result.data(), 1, result.size(), stdout) != result.size()) fail("write-report", ERROR_WRITE_FAULT);
}
std::wstring native(const std::wstring& path) {
    if (!vse::absolute_local_path_ok(path)) fail("invalid-internal-path");
    return L"\\\\?\\" + path;
}
BY_HANDLE_FILE_INFORMATION information(HANDLE file) {
    BY_HANDLE_FILE_INFORMATION result{};
    if (!GetFileInformationByHandle(file, &result)) winfail("inspect-identity");
    if (result.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) fail("reparse-point-rejected");
    return result;
}
bool same(const BY_HANDLE_FILE_INFORMATION& a, const BY_HANDLE_FILE_INFORMATION& b) {
    return a.dwVolumeSerialNumber == b.dwVolumeSerialNumber && a.nFileIndexHigh == b.nFileIndexHigh && a.nFileIndexLow == b.nFileIndexLow;
}
std::string identity(const BY_HANDLE_FILE_INFORMATION& value) {
    return "{\"volume_serial\":" + std::to_string(value.dwVolumeSerialNumber) +
        ",\"file_index_high\":" + std::to_string(value.nFileIndexHigh) +
        ",\"file_index_low\":" + std::to_string(value.nFileIndexLow) + '}';
}
Handle directory(const std::wstring& path, bool rename = false) {
    Handle result(CreateFileW(native(path).c_str(), FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | (rename ? DELETE : 0),
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS, nullptr));
    if (!result.valid()) winfail("open-pinned-directory");
    if (!(information(result.get()).dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) fail("not-a-directory");
    return result;
}
std::wstring canonical(HANDLE file) {
    std::vector<wchar_t> buffer(32768);
    const DWORD count = GetFinalPathNameByHandleW(file, buffer.data(), static_cast<DWORD>(buffer.size()), FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!count || count >= buffer.size()) winfail("canonical-directory");
    return vse::ordinary_os_path(std::wstring(buffer.data(), count));
}
bool exists(const std::wstring& path) {
    const DWORD attributes = GetFileAttributesW(native(path).c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES) {
        const DWORD error = GetLastError();
        if (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND) return false;
        fail("path-presence", error);
    }
    if (attributes & FILE_ATTRIBUTE_REPARSE_POINT) fail("presence-reparse-point");
    return true;
}
BY_HANDLE_FILE_INFORMATION path_identity(const std::wstring& path) {
    // This query handle must share DELETE with the still-held source DELETE
    // handle after rename. It grants no DELETE access and does not replace the
    // original no-share-delete source/ancestor locks.
    Handle file(CreateFileW(native(path).c_str(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!file.valid()) winfail("query-path-identity");
    return information(file.get());
}
struct Locks {
    std::vector<Handle> handles;
    std::vector<BY_HANDLE_FILE_INFORMATION> before;
    void add(const std::wstring& path) {
        auto handle = directory(path);
        before.push_back(information(handle.get()));
        handles.push_back(std::move(handle));
    }
    bool unchanged() const {
        for (std::size_t i = 0; i < handles.size(); ++i)
            if (!same(before[i], information(handles[i].get()))) return false;
        return true;
    }
};
std::wstring pin_parent(const std::wstring& supplied, Locks& locks) {
    if (!vse::absolute_local_path_ok(supplied)) fail("user-path-must-be-ordinary-local-absolute");
    const auto drive = supplied.substr(0, 3);
    if (GetDriveTypeW(drive.c_str()) != DRIVE_FIXED) fail("fixed-local-drive-required");
    wchar_t mapping[32768]{};
    if (!QueryDosDeviceW(supplied.substr(0, 2).c_str(), mapping, 32768)) winfail("query-drive");
    if (std::wstring(mapping).rfind(L"\\Device\\HarddiskVolume", 0) != 0) fail("substituted-drive-rejected");
    std::wstring current = drive;
    locks.add(current);
    std::size_t start = 3;
    while (start < supplied.size()) {
        const auto end = supplied.find(L'\\', start);
        current = vse::join(current, supplied.substr(start, end == std::wstring::npos ? end : end - start));
        locks.add(current);
        if (end == std::wstring::npos) break;
        start = end + 1;
    }
    return canonical(locks.handles.back().get());
}
void make_directory(const std::wstring& path) {
    if (!CreateDirectoryW(native(path).c_str(), nullptr)) winfail("create-new-synthetic-directory");
    auto check = directory(path);
    if (!check.valid()) fail("created-directory-not-held");
}
void make_marker(const std::wstring& path, const char* payload) {
    Handle file(CreateFileW(native(path).c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!file.valid()) winfail("create-new-synthetic-marker");
    DWORD count = 0;
    const auto bytes = static_cast<DWORD>(std::strlen(payload));
    if (!WriteFile(file.get(), payload, bytes, &count, nullptr) || count != bytes || !FlushFileBuffers(file.get())) winfail("write-synthetic-marker");
}
bool marker_matches(const std::wstring& path, const char* payload) {
    Handle file(CreateFileW(native(path).c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!file.valid()) return false;
    const auto detail = information(file.get());
    if (detail.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY || detail.nNumberOfLinks != 1) return false;
    char buffer[128]{};
    DWORD count = 0;
    if (!ReadFile(file.get(), buffer, static_cast<DWORD>(sizeof(buffer)), &count, nullptr)) winfail("read-synthetic-marker");
    return count == std::strlen(payload) && std::memcmp(buffer, payload, count) == 0;
}
const char* const modes[] = {"relative-exact-buffer", "relative-nul-buffer", "absolute-extended-nul-buffer"};
const char* const source_bytes = "VSE SYNTHETIC SOURCE MARKER. NOT CUSTOMER DATA.\n";
const char* const sentinel_bytes = "VSE SYNTHETIC EXISTING TARGET. MUST NOT CHANGE.\n";
bool probe(unsigned index, const std::wstring& requested_root, Record& row) {
    const bool existing = (index & 1) != 0;
    const bool long_parent = (index & 2) != 0;
    const bool deep_child = (index & 4) != 0;
    const bool unicode = (index & 8) != 0;
    const unsigned mode = index / 16;
    text(row, "mode", modes[mode]);
    boolean(row, "existing_target", existing); boolean(row, "long_parent", long_parent);
    boolean(row, "deep_child", deep_child); boolean(row, "unicode", unicode);
    if (!vse::absolute_local_path_ok(requested_root) || requested_root.size() > 4096) fail("invalid-new-root");
    const auto slash = requested_root.rfind(L'\\');
    if (slash < 2 || slash == std::wstring::npos) fail("invalid-new-root-parent");
    Locks locks;
    const auto parent = pin_parent(slash == 2 ? requested_root.substr(0, 3) : requested_root.substr(0, slash), locks);
    const auto root = vse::join(parent, requested_root.substr(slash + 1));
    make_directory(root); // Must be NEW; never reuses or deletes any existing tree.
    locks.add(root);
    row["new_root"] = quoted(root);
    auto destination_parent = root;
    if (long_parent) {
        for (unsigned part = 0; part < 3; ++part) {
            destination_parent = vse::join(destination_parent, std::wstring(110, static_cast<wchar_t>(L'p' + part)));
            make_directory(destination_parent); locks.add(destination_parent);
        }
    }
    const HANDLE destination_parent_handle = locks.handles.back().get();
    const auto destination_parent_before = information(destination_parent_handle);
    const auto stage = vse::join(destination_parent, L".vse-incomplete-synthetic");
    make_directory(stage); locks.add(stage);
    const auto extracted = vse::join(stage, L"payload");
    make_directory(extracted); locks.add(extracted);
    const auto source = vse::join(extracted, L"source-root");
    make_directory(source);
    auto source_handle = directory(source, true);
    const auto source_before = information(source_handle.get());
    std::wstring relative;
    if (deep_child) {
        for (unsigned part = 0; part < 5; ++part) {
            const std::wstring component = L"deep-" + std::wstring(64, static_cast<wchar_t>(L'a' + part));
            relative = relative.empty() ? component : vse::join(relative, component);
            make_directory(vse::join(source, relative));
        }
    }
    const auto marker_leaf = unicode ? L"marker \u4e2d\u6587 \u03a9 &!%().txt" : L"marker.txt";
    relative = relative.empty() ? marker_leaf : vse::join(relative, marker_leaf);
    const auto source_marker = vse::join(source, relative);
    make_marker(source_marker, source_bytes);
    const auto marker_before = path_identity(source_marker);
    if (!marker_matches(source_marker, source_bytes)) fail("source-marker-before-rename");
    const std::wstring leaf = unicode ? L"renamed \u4e2d\u6587 \u03a9 &!%()" : L"renamed";
    const auto destination = vse::join(destination_parent, leaf);
    BY_HANDLE_FILE_INFORMATION target_before{}, sentinel_before{};
    const auto sentinel = vse::join(destination, L"EXISTING-TARGET-SENTINEL.txt");
    if (existing) {
        make_directory(destination);
        make_marker(sentinel, sentinel_bytes);
        target_before = path_identity(destination); sentinel_before = path_identity(sentinel);
        // No destination/sentinel handle remains open during rename: otherwise
        // a sharing violation could mask a broken ReplaceIfExists=FALSE check.
    }
    row["source_path"] = quoted(source); row["destination_path"] = quoted(destination);
    row["relative_marker"] = quoted(relative);
    number(row, "source_path_utf16_units", source.size()); number(row, "destination_path_utf16_units", destination.size());
    number(row, "source_marker_path_utf16_units", source_marker.size());
    row["source_identity_before"] = identity(source_before); row["marker_identity_before"] = identity(marker_before);
    row["destination_parent_identity_before"] = identity(destination_parent_before);
    if (existing) { row["existing_target_identity_before"] = identity(target_before); row["existing_sentinel_identity_before"] = identity(sentinel_before); }
    const auto filename = mode == 2 ? native(destination) : leaf;
    const auto filename_bytes = filename.size() * sizeof(wchar_t);
    const auto trailing_bytes = mode == 0 ? 0 : sizeof(wchar_t);
    std::vector<unsigned char> buffer(offsetof(FILE_RENAME_INFO, FileName) + filename_bytes + trailing_bytes, 0);
    auto rename = reinterpret_cast<FILE_RENAME_INFO*>(buffer.data());
    rename->ReplaceIfExists = FALSE;
    rename->RootDirectory = mode == 2 ? nullptr : destination_parent_handle;
    rename->FileNameLength = static_cast<DWORD>(filename_bytes);
    std::memcpy(rename->FileName, filename.data(), filename_bytes);
    number(row, "file_name_length_bytes", filename_bytes); number(row, "rename_buffer_bytes", buffer.size());
    number(row, "file_name_offset", offsetof(FILE_RENAME_INFO, FileName));
    number(row, "root_directory_offset", offsetof(FILE_RENAME_INFO, RootDirectory));
    number(row, "file_name_length_offset", offsetof(FILE_RENAME_INFO, FileNameLength));
    number(row, "file_rename_info_sizeof", sizeof(FILE_RENAME_INFO));
    number(row, "pointer_bytes", sizeof(void*));
    number(row, "trailing_nul_bytes", trailing_bytes); boolean(row, "root_directory_is_null", mode == 2);
    boolean(row, "replace_if_exists", false);
    SetLastError(ERROR_SUCCESS);
    const BOOL renamed = SetFileInformationByHandle(source_handle.get(), FileRenameInfo, rename, static_cast<DWORD>(buffer.size()));
    const DWORD rename_error = renamed ? ERROR_SUCCESS : GetLastError();
    boolean(row, "rename_returned_success", renamed != FALSE); number(row, "rename_win32_error", rename_error);
    row["source_identity_after"] = identity(information(source_handle.get()));
    row["source_handle_final_path"] = quoted(canonical(source_handle.get()));
    row["destination_parent_identity_after"] = identity(information(destination_parent_handle));
    const bool parents_stable = locks.unchanged();
    const bool source_present = exists(source), target_present = exists(destination);
    boolean(row, "ancestor_identities_unchanged", parents_stable);
    boolean(row, "source_present_after", source_present); boolean(row, "target_present_after", target_present);
    bool marker_ok = false, identities_ok = false, target_preserved = !existing;
    const auto actual_marker = vse::join(renamed ? destination : source, relative);
    if (exists(actual_marker)) {
        marker_ok = marker_matches(actual_marker, source_bytes);
        const auto marker_after = path_identity(actual_marker);
        row["marker_identity_after"] = identity(marker_after);
        identities_ok = same(marker_before, marker_after) && same(source_before, information(source_handle.get()));
    }
    if (existing && target_present && exists(sentinel)) {
        const auto target_after = path_identity(destination), sentinel_after = path_identity(sentinel);
        row["existing_target_identity_after"] = identity(target_after); row["existing_sentinel_identity_after"] = identity(sentinel_after);
        target_preserved = same(target_before, target_after) && same(sentinel_before, sentinel_after) && marker_matches(sentinel, sentinel_bytes);
    }
    boolean(row, "source_marker_bytes_unchanged", marker_ok); boolean(row, "source_and_marker_identities_unchanged", identities_ok);
    boolean(row, "existing_target_preserved", target_preserved);
    const bool correct = parents_stable && marker_ok && identities_ok && (existing ? (!renamed && source_present && target_preserved) : (renamed && !source_present && target_present));
    boolean(row, "expectation_met", correct);
    text(row, "observation", correct ? (existing ? "correct-refusal" : "correct-rename") : "expectation-not-met");
    return correct;
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    // Process-local diagnostic behavior only; avoids unattended Server dialogs.
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    Record row;
    number(row, "schema", 1);
    text(row, "scope", "synthetic-rename-api-observation-not-production-or-client-certification");
    boolean(row, "client_restore_verified", false);
    unsigned index = 48;
    try {
        if (argc != 5 || std::wstring(argv[1]) != L"--new-root" || std::wstring(argv[3]) != L"--case") fail("usage-new-root-and-case-required");
        const std::wstring digits(argv[4]);
        if (digits.empty() || digits.size() > 2) fail("case-range");
        index = 0;
        for (wchar_t value : digits) {
            if (value < L'0' || value > L'9') fail("case-digits");
            index = index * 10 + static_cast<unsigned>(value - L'0');
        }
        if (index >= 48) fail("case-range");
        number(row, "case_index", index);
        const bool correct = probe(index, argv[2], row);
        emit(row);
        return correct ? 0 : 2;
    } catch (const Failure& error) {
        text(row, "error_stage", error.stage.c_str()); number(row, "error_win32", error.error);
    } catch (const std::exception& error) {
        text(row, "exception", error.what());
    }
    boolean(row, "expectation_met", false);
    try { emit(row); } catch (...) { return 4; }
    return 3;
}
