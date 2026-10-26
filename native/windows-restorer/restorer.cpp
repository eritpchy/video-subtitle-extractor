// SPDX-License-Identifier: Apache-2.0
// Native Windows x64 only. No networking, installer, elevation or shell.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <sddl.h>
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdio>
#include <cwchar>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <vector>
#include "core.hpp"
#include "bundle_metadata.hpp" // Generated only by generate_metadata.py.
#pragma comment(lib, "bcrypt.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "user32.lib")

namespace {
struct Failure { std::wstring message; };
[[noreturn]] void fail(const std::wstring& message) { throw Failure{message}; }
[[noreturn]] void winfail(const std::wstring& message) { fail(message + L" (Windows error " + std::to_wstring(GetLastError()) + L")"); }
volatile LONG cancelled = 0;
BOOL WINAPI console_control(DWORD event) {
    if (event == CTRL_C_EVENT || event == CTRL_BREAK_EVENT || event == CTRL_CLOSE_EVENT || event == CTRL_SHUTDOWN_EVENT) {
        InterlockedExchange(&cancelled, 1); return TRUE;
    }
    return FALSE;
}
void check_cancel() { if (InterlockedCompareExchange(&cancelled, 0, 0)) fail(L"Restoration interrupted; nothing was published."); }
class Handle {
    HANDLE value_ = INVALID_HANDLE_VALUE;
public:
    Handle() = default;
    explicit Handle(HANDLE value) : value_(value) {}
    ~Handle() { reset(); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    Handle(Handle&& other) noexcept : value_(other.value_) { other.value_ = INVALID_HANDLE_VALUE; }
    Handle& operator=(Handle&& other) noexcept { if (this != &other) { reset(); value_ = other.value_; other.value_ = INVALID_HANDLE_VALUE; } return *this; }
    bool valid() const { return value_ && value_ != INVALID_HANDLE_VALUE; }
    HANDLE get() const { return value_; }
    void reset() { if (valid()) CloseHandle(value_); value_ = INVALID_HANDLE_VALUE; }
};
struct LocalFreeDeleter { void operator()(void* value) const { if (value) LocalFree(value); } };
using LocalMemory = std::unique_ptr<void, LocalFreeDeleter>;

Handle inheritable_duplicate(HANDLE original) {
    HANDLE duplicate = nullptr;
    if (!DuplicateHandle(GetCurrentProcess(), original, GetCurrentProcess(), &duplicate,
                         0, TRUE, DUPLICATE_SAME_ACCESS)) winfail(L"Cannot prepare explicit child handle");
    return Handle(duplicate);
}
Handle child_output(DWORD standard) {
    const HANDLE original = GetStdHandle(standard);
    if (original && original != INVALID_HANDLE_VALUE) return inheritable_duplicate(original);
    // A double-click may have no console. This fixed OS null device is never
    // derived from a user path; it is not a general device-path allowance.
    SECURITY_ATTRIBUTES attributes{sizeof(SECURITY_ATTRIBUTES), nullptr, TRUE};
    Handle output(CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                              &attributes, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr));
    if (!output.valid()) winfail(L"Cannot prepare child diagnostic output");
    return output;
}
class ChildAttributes {
    std::vector<unsigned char> storage_;
    LPPROC_THREAD_ATTRIBUTE_LIST list_ = nullptr;
public:
    ChildAttributes() {
        SIZE_T bytes = 0;
        if (InitializeProcThreadAttributeList(nullptr, 1, 0, &bytes) ||
            GetLastError() != ERROR_INSUFFICIENT_BUFFER || !bytes) winfail(L"Cannot size child handle allowlist");
        storage_.resize(bytes);
        auto list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(storage_.data());
        if (!InitializeProcThreadAttributeList(list, 1, 0, &bytes)) winfail(L"Cannot initialize child handle allowlist");
        list_ = list;
    }
    ~ChildAttributes() { if (list_) DeleteProcThreadAttributeList(list_); }
    ChildAttributes(const ChildAttributes&) = delete;
    ChildAttributes& operator=(const ChildAttributes&) = delete;
    LPPROC_THREAD_ATTRIBUTE_LIST get() const { return list_; }
    void allow(std::array<HANDLE, 3>& handles) {
        if (!UpdateProcThreadAttribute(list_, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                       handles.data(), handles.size() * sizeof(HANDLE), nullptr, nullptr)) winfail(L"Cannot restrict child handle inheritance");
    }
};

// Prefix only already validated internal paths. User-supplied device/UNC
// namespace paths never reach this conversion; no LongPathsEnabled change.
std::wstring native_path(const std::wstring& path) {
    if (!vse::absolute_local_path_ok(path)) fail(L"Invalid internal local path");
    return L"\\\\?\\" + path;
}
std::wstring normalize_handle_path(HANDLE handle) {
    const DWORD needed = GetFinalPathNameByHandleW(handle, nullptr, 0, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!needed || needed > 32767) winfail(L"Cannot resolve local directory identity");
    std::vector<wchar_t> value(static_cast<std::size_t>(needed) + 1);
    const DWORD count = GetFinalPathNameByHandleW(handle, value.data(), static_cast<DWORD>(value.size()), FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (!count || count >= value.size()) winfail(L"Cannot resolve local directory identity");
    std::wstring path(value.data(), count);
    if (path.rfind(L"\\\\?\\", 0) != 0) fail(L"Unexpected directory namespace");
    path.erase(0, 4); // Internal API result only. User device paths are rejected.
    if (!vse::absolute_local_path_ok(path)) fail(L"Resolved path is not a supported local path");
    return path;
}
BY_HANDLE_FILE_INFORMATION info(HANDLE handle) {
    BY_HANDLE_FILE_INFORMATION result{};
    if (!GetFileInformationByHandle(handle, &result)) winfail(L"Cannot inspect file identity");
    if (result.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) fail(L"Reparse points, symbolic links and junctions are not accepted");
    return result;
}
Handle open_directory(const std::wstring& path, bool rename_access = false) {
    Handle result(CreateFileW(native_path(path).c_str(), FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | (rename_access ? DELETE : 0),
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (!result.valid()) winfail(L"Cannot safely open directory: " + path);
    if (!(info(result.get()).dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) fail(L"Expected directory: " + path);
    return result;
}
struct DirectoryChain { std::wstring path; std::vector<Handle> locks; };
DirectoryChain pin_directory(const std::wstring& supplied) {
    if (!vse::absolute_local_path_ok(supplied)) fail(L"Use a local absolute path without links, device names, trailing separators or ambiguous components: " + supplied);
    const std::wstring drive = supplied.substr(0, 3);
    DWORD volume_flags = 0;
    if (!GetVolumeInformationW(drive.c_str(), nullptr, 0, nullptr, nullptr, &volume_flags, nullptr, 0)) winfail(L"Cannot inspect filesystem capabilities");
    if (!(volume_flags & FILE_PERSISTENT_ACLS)) fail(L"A local filesystem supporting private access controls is required");
    if (GetDriveTypeW(drive.c_str()) != DRIVE_FIXED) fail(L"Only a local fixed drive is supported (not UNC, network, removable or substituted drives)");
    wchar_t target[32768]{};
    if (!QueryDosDeviceW(supplied.substr(0, 2).c_str(), target, 32768)) winfail(L"Cannot inspect drive mapping");
    if (std::wstring(target).rfind(L"\\Device\\HarddiskVolume", 0) != 0) fail(L"Substituted or non-local drive mapping is not accepted");
    DirectoryChain result;
    result.locks.push_back(open_directory(drive));
    std::wstring current = drive;
    std::size_t start = 3;
    while (start < supplied.size()) {
        const auto end = supplied.find(L'\\', start);
        current = vse::join(current, supplied.substr(start, end == std::wstring::npos ? end : end - start));
        result.locks.push_back(open_directory(current));
        if (end == std::wstring::npos) break;
        start = end + 1;
    }
    // 8.3 aliases (e.g. RUNNER~1) are legitimate; the held ancestor handles,
    // not a lexical equality/prefix test, establish the path boundary.
    result.path = normalize_handle_path(result.locks.back().get());
    return result;
}
std::wstring executable_parent() {
    std::vector<wchar_t> buffer(32768);
    const DWORD count = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!count || count >= buffer.size()) winfail(L"Cannot locate restorer");
    std::wstring path = vse::ordinary_os_path(std::wstring(buffer.data(), count));
    const auto split = path.rfind(L'\\');
    if (split == std::wstring::npos) fail(L"Restorer has no local directory");
    return split == 2 ? path.substr(0, 3) : path.substr(0, split);
}
Handle open_input(const std::wstring& path, bool allow_system_hardlinks = false) {
    Handle result(CreateFileW(native_path(path).c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
    if (!result.valid()) winfail(L"Cannot safely open required file: " + path);
    auto detail = info(result.get());
    if (detail.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY || (!allow_system_hardlinks && detail.nNumberOfLinks != 1)) fail(L"Expected a regular, non-hard-linked file: " + path);
    return result;
}
std::uint64_t file_size(HANDLE handle) {
    LARGE_INTEGER value{};
    if (!GetFileSizeEx(handle, &value) || value.QuadPart < 0) winfail(L"Cannot read file size");
    return static_cast<std::uint64_t>(value.QuadPart);
}
class Sha256 {
    BCRYPT_ALG_HANDLE provider_ = nullptr;
    BCRYPT_HASH_HANDLE hash_ = nullptr;
    std::vector<UCHAR> object_;
    void clear() { if (hash_) BCryptDestroyHash(hash_); if (provider_) BCryptCloseAlgorithmProvider(provider_, 0); hash_ = nullptr; provider_ = nullptr; }
public:
    Sha256() {
        if (BCryptOpenAlgorithmProvider(&provider_, BCRYPT_SHA256_ALGORITHM, nullptr, 0) < 0) fail(L"Windows SHA256 provider unavailable");
        DWORD bytes = 0, got = 0;
        if (BCryptGetProperty(provider_, BCRYPT_OBJECT_LENGTH, reinterpret_cast<PUCHAR>(&bytes), sizeof(bytes), &got, 0) < 0 || got != sizeof(bytes)) { clear(); fail(L"Cannot initialize SHA256"); }
        object_.resize(bytes);
        if (BCryptCreateHash(provider_, &hash_, object_.data(), bytes, nullptr, 0, 0) < 0) { clear(); fail(L"Cannot create SHA256 state"); }
    }
    ~Sha256() { clear(); }
    Sha256(const Sha256&) = delete;
    Sha256& operator=(const Sha256&) = delete;
    void add(const void* data, DWORD bytes) { if (BCryptHashData(hash_, reinterpret_cast<PUCHAR>(const_cast<void*>(data)), bytes, 0) < 0) fail(L"SHA256 update failed"); }
    std::string finish() {
        std::array<UCHAR, 32> digest{};
        if (BCryptFinishHash(hash_, digest.data(), static_cast<ULONG>(digest.size()), 0) < 0) fail(L"SHA256 finalization failed");
        const char* alphabet = "0123456789abcdef";
        std::string result;
        for (auto value : digest) { result.push_back(alphabet[value >> 4]); result.push_back(alphabet[value & 15]); }
        return result;
    }
};
void rewind_file(HANDLE file) { LARGE_INTEGER zero{}; if (!SetFilePointerEx(file, zero, nullptr, FILE_BEGIN)) winfail(L"Cannot rewind file"); }
DWORD read_chunk(HANDLE file, std::vector<unsigned char>& buffer) {
    DWORD got = 0;
    if (!ReadFile(file, buffer.data(), static_cast<DWORD>(buffer.size()), &got, nullptr)) winfail(L"File read failed");
    check_cancel(); return got;
}
void write_chunk(HANDLE file, const unsigned char* data, DWORD count) {
    DWORD done = 0;
    while (done < count) {
        DWORD wrote = 0;
        if (!WriteFile(file, data + done, count - done, &wrote, nullptr) || !wrote) winfail(L"File write failed (possibly insufficient disk space)");
        done += wrote; check_cancel();
    }
}
void verify_file(HANDLE file, std::uint64_t bytes, const char* expected, std::vector<unsigned char>& buffer) {
    if (file_size(file) != bytes) fail(L"Unexpected file size");
    rewind_file(file); Sha256 hash; std::uint64_t total = 0;
    for (;;) { const DWORD got = read_chunk(file, buffer); if (!got) break; total = vse::checked_add(total, got); hash.add(buffer.data(), got); }
    if (total != bytes || hash.finish() != expected) fail(L"SHA256 mismatch; nothing was published");
}
std::wstring random_suffix() {
    std::array<UCHAR, 16> bytes{};
    if (BCryptGenRandom(nullptr, bytes.data(), static_cast<ULONG>(bytes.size()), BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0) fail(L"Cannot generate a private staging name");
    const wchar_t* alphabet = L"0123456789abcdef";
    std::wstring text;
    for (auto value : bytes) { text.push_back(alphabet[value >> 4]); text.push_back(alphabet[value & 15]); }
    return text;
}
LocalMemory private_descriptor() {
    HANDLE raw = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &raw)) winfail(L"Cannot inspect current user");
    Handle token(raw); DWORD bytes = 0;
    GetTokenInformation(token.get(), TokenUser, nullptr, 0, &bytes);
    if (!bytes) winfail(L"Cannot size current user identity");
    std::vector<unsigned char> storage(bytes);
    if (!GetTokenInformation(token.get(), TokenUser, storage.data(), bytes, &bytes)) winfail(L"Cannot read current user identity");
    LPWSTR sid = nullptr;
    if (!ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(storage.data())->User.Sid, &sid)) winfail(L"Cannot encode current user identity");
    LocalMemory sid_memory(sid);
    const std::wstring sddl = std::wstring(L"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;") + sid + L")";
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1, &descriptor, nullptr)) winfail(L"Cannot create private staging permissions");
    return LocalMemory(descriptor);
}
std::wstring create_stage(const std::wstring& parent) {
    auto descriptor = private_descriptor();
    SECURITY_ATTRIBUTES attributes{sizeof(SECURITY_ATTRIBUTES), descriptor.get(), FALSE};
    for (unsigned attempt = 0; attempt < 16; ++attempt) {
        std::wstring stage = vse::join(parent, L".vse-incomplete-" + random_suffix());
        if (!vse::absolute_local_path_ok(stage)) fail(L"Destination path is too long; choose a shorter local folder");
        if (CreateDirectoryW(native_path(stage).c_str(), &attributes)) return stage;
        if (GetLastError() != ERROR_ALREADY_EXISTS) winfail(L"Cannot create private staging directory");
    }
    fail(L"Cannot allocate a unique staging directory");
}
void assert_absent(const std::wstring& path) {
    if (GetFileAttributesW(native_path(path).c_str()) != INVALID_FILE_ATTRIBUTES) fail(L"Destination already exists; no existing data will be overwritten: " + path);
    const DWORD error = GetLastError();
    if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PATH_NOT_FOUND) winfail(L"Cannot establish that destination is absent");
}
void check_space(const std::wstring& parent) {
    ULARGE_INTEGER available{}, total{}, free{};
    if (!GetDiskFreeSpaceExW(native_path(parent).c_str(), &available, &total, &free)) winfail(L"Cannot inspect available disk space");
    const auto needed = vse::required_space(bundle::archive_bytes, bundle::uncompressed_bytes, bundle::entry_count);
    if (available.QuadPart < needed) fail(L"Insufficient free space: need at least " + std::to_wstring(needed) + L" bytes in the destination (downloaded parts are additional).");
}
void extract_with_system_tar(HANDLE verified_zip, const std::wstring& destination) {
    wchar_t directory[MAX_PATH + 1]{};
    const UINT count = GetSystemDirectoryW(directory, MAX_PATH + 1);
    if (!count || count > MAX_PATH) winfail(L"Cannot locate Windows system directory");
    auto system = pin_directory(directory);
    const std::wstring tar = vse::join(system.path, L"tar.exe");
    auto tar_lock = open_input(tar, true); // Existence, regular file and no mutation while launching.
    wchar_t windows[MAX_PATH + 1]{};
    const UINT win_count = GetWindowsDirectoryW(windows, MAX_PATH + 1);
    if (!win_count || win_count > MAX_PATH) winfail(L"Cannot locate Windows directory");
    // Do not inherit TAR_OPTIONS, PATH, Python/Conda variables or shell hooks.
    std::wstring environment = std::wstring(L"SystemRoot=") + windows;
    environment.push_back(L'\0'); environment += std::wstring(L"WINDIR=") + windows;
    environment.push_back(L'\0'); environment.push_back(L'\0');
    // Some OS bsdtar versions use narrow argv even when the caller uses
    // CreateProcessW. Do not send either Unicode path through that conversion.
    // stdin is a seekable duplicate of the SAME fully verified, read-only,
    // no-write/no-delete ZIP handle, not a pipe or a pathname reopened by tar.
    rewind_file(verified_zip);
    auto input = inheritable_duplicate(verified_zip);
    auto output = child_output(STD_OUTPUT_HANDLE), error = child_output(STD_ERROR_HANDLE);
    std::array<HANDLE, 3> inherited{input.get(), output.get(), error.get()};
    ChildAttributes attributes; attributes.allow(inherited);
    const auto working_directory = native_path(destination);
    std::wstring command = vse::quote_arg(tar) + L" -x --no-same-owner --no-same-permissions -f -";
    Handle job(CreateJobObjectW(nullptr, nullptr));
    if (!job.valid()) winfail(L"Cannot create extraction job");
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!SetInformationJobObject(job.get(), JobObjectExtendedLimitInformation, &limits, sizeof(limits))) winfail(L"Cannot bind extraction lifetime");
    STARTUPINFOEXW startup{}; startup.StartupInfo.cb = sizeof(startup);
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.get(); startup.StartupInfo.hStdOutput = output.get();
    startup.StartupInfo.hStdError = error.get(); startup.lpAttributeList = attributes.get();
    PROCESS_INFORMATION process{};
    // The empty private target itself (still ancestor-locked by the caller) is
    // the UTF-16 cwd. No ASCII staging relocation or -C argument is involved.
    // Only the above three handles inherit; no source/ancestor locks escape.
    if (!CreateProcessW(tar.c_str(), command.data(), nullptr, nullptr, TRUE,
                        CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                        environment.data(), working_directory.c_str(), &startup.StartupInfo, &process)) winfail(L"Cannot start system tar; no dependency will be downloaded");
    Handle process_handle(process.hProcess), thread_handle(process.hThread);
    if (!AssignProcessToJobObject(job.get(), process_handle.get())) { TerminateProcess(process_handle.get(), 1); winfail(L"Cannot bind system tar to restoration job"); }
    if (ResumeThread(thread_handle.get()) == static_cast<DWORD>(-1)) winfail(L"Cannot resume system tar");
    for (;;) {
        check_cancel();
        const DWORD wait = WaitForSingleObject(process_handle.get(), 100);
        if (wait == WAIT_OBJECT_0) break;
        if (wait != WAIT_TIMEOUT) winfail(L"Cannot wait for system tar");
    }
    DWORD status = 1;
    if (!GetExitCodeProcess(process_handle.get(), &status)) winfail(L"Cannot inspect system tar result");
    if (status != 0) fail(L"System tar could not extract the verified ZIP (exit " + std::to_wstring(status) + L"); nothing was published.");
}
struct TreeLocks { std::vector<Handle> directories; std::vector<Handle> files; };
void check_tree(const std::wstring& root, const std::wstring& relative, std::map<std::wstring, const vse::Entry*>& expected,
                TreeLocks& locks, std::vector<unsigned char>& buffer) {
    check_cancel();
    if (std::count(relative.begin(), relative.end(), L'\\') > 255) fail(L"Restored directory depth exceeds the fixed bound");
    const auto directory = relative.empty() ? root : vse::join(root, relative);
    WIN32_FIND_DATAW found{};
    HANDLE raw = FindFirstFileW((native_path(directory) + L"\\*").c_str(), &found);
    if (raw == INVALID_HANDLE_VALUE) winfail(L"Cannot enumerate restored directory");
    struct FindCloser { HANDLE h; ~FindCloser() { FindClose(h); } } close{raw};
    do {
        const std::wstring name(found.cFileName);
        if (name == L"." || name == L"..") continue;
        if (!vse::component_ok(name) || found.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) fail(L"Unsafe restored file or reparse point");
        const auto key = relative.empty() ? name : vse::join(relative, name);
        auto it = expected.find(key);
        if (it == expected.end()) fail(L"Unexpected restored entry: " + key);
        const auto entry = it->second; expected.erase(it);
        const bool directory_entry = (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        if (directory_entry != entry->directory) fail(L"Restored entry type differs from accepted ZIP: " + key);
        if (directory_entry) {
            if (key != bundle::root_name) locks.directories.push_back(open_directory(vse::join(root, key)));
            check_tree(root, key, expected, locks, buffer);
        } else {
            locks.files.push_back(open_input(vse::join(root, key)));
            verify_file(locks.files.back().get(), entry->bytes, entry->sha256, buffer);
        }
    } while (FindNextFileW(raw, &found));
    if (GetLastError() != ERROR_NO_MORE_FILES) winfail(L"Cannot finish enumerating restored directory");
}
void rename_without_replace(HANDLE root, HANDLE parent, const std::wstring& destination) {
    const auto bytes = destination.size() * sizeof(wchar_t);
    std::vector<unsigned char> storage(offsetof(FILE_RENAME_INFO, FileName) + bytes);
    auto rename = reinterpret_cast<FILE_RENAME_INFO*>(storage.data());
    rename->ReplaceIfExists = FALSE; rename->RootDirectory = parent;
    rename->FileNameLength = static_cast<DWORD>(bytes);
    memcpy(rename->FileName, destination.data(), bytes);
    if (!SetFileInformationByHandle(root, FileRenameInfo, rename, static_cast<DWORD>(storage.size()))) winfail(L"Cannot atomically publish restored directory; existing data was not replaced");
}
void remove_owned_file(HANDLE file) {
    FILE_DISPOSITION_INFO disposition{TRUE};
    if (!SetFileInformationByHandle(file, FileDispositionInfo, &disposition, sizeof(disposition))) winfail(L"Cannot remove owned temporary ZIP");
}
void restore(const std::wstring& source_path, const std::wstring& destination_path, std::wstring& incomplete) {
    auto source = pin_directory(source_path);
    auto parent = pin_directory(destination_path);
    const auto final_path = vse::join(parent.path, bundle::root_name);
    if (!vse::absolute_local_path_ok(final_path) || parent.path.size() + 64 + bundle::max_entry_path >= 32700) fail(L"Destination is too long; choose a shorter local folder");
    assert_absent(final_path); check_space(parent.path);
    std::vector<Handle> parts;
    for (const auto& part : bundle::parts) {
        auto input = open_input(vse::join(source.path, part.name));
        if (file_size(input.get()) != part.bytes) fail(L"Missing/truncated/incorrect volume: " + std::wstring(part.name));
        parts.push_back(std::move(input));
    }
    incomplete = create_stage(parent.path);
    auto staging = open_directory(incomplete);
    const auto temporary_zip = vse::join(incomplete, L"verified-archive.zip");
    Handle output(CreateFileW(native_path(temporary_zip).c_str(), GENERIC_READ | GENERIC_WRITE | DELETE, 0, nullptr, CREATE_NEW,
                              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN, nullptr));
    if (!output.valid()) winfail(L"Cannot create temporary ZIP");
    std::vector<unsigned char> buffer(1024 * 1024);
    Sha256 archive_hash; std::uint64_t total = 0;
    for (std::size_t index = 0; index < parts.size(); ++index) {
        Sha256 part_hash; std::uint64_t part_total = 0;
        for (;;) {
            const DWORD got = read_chunk(parts[index].get(), buffer); if (!got) break;
            part_total = vse::checked_add(part_total, got); total = vse::checked_add(total, got);
            if (part_total > bundle::parts[index].bytes || total > bundle::archive_bytes) fail(L"Unexpected volume length");
            part_hash.add(buffer.data(), got); archive_hash.add(buffer.data(), got); write_chunk(output.get(), buffer.data(), got);
        }
        if (part_total != bundle::parts[index].bytes || part_hash.finish() != bundle::parts[index].sha256) fail(L"Volume SHA256 mismatch: " + std::wstring(bundle::parts[index].name));
    }
    if (total != bundle::archive_bytes || archive_hash.finish() != bundle::archive_sha256) fail(L"Reconstructed ZIP differs from the accepted archive");
    if (!FlushFileBuffers(output.get())) winfail(L"Cannot flush reconstructed ZIP");
    verify_file(output.get(), bundle::archive_bytes, bundle::archive_sha256, buffer); // Re-read actual disk bytes.
    // Reopen read-only under the private stage after verification. Re-verify on
    // the retained no-write/no-delete handle before giving tar a stdin duplicate.
    output.reset();
    auto verified_zip = open_input(temporary_zip);
    const auto owned_zip_identity = info(verified_zip.get());
    verify_file(verified_zip.get(), bundle::archive_bytes, bundle::archive_sha256, buffer);
    const auto extracted = vse::join(incomplete, L"payload");
    if (!CreateDirectoryW(native_path(extracted).c_str(), nullptr)) winfail(L"Cannot create empty extraction directory");
    auto extraction_directory = open_directory(extracted);
    extract_with_system_tar(verified_zip.get(), extracted);
    const auto restored = vse::join(extracted, bundle::root_name);
    auto restored_root = open_directory(restored, true);
    std::map<std::wstring, const vse::Entry*> expected;
    for (const auto& entry : bundle::entries) expected.emplace(entry.name, &entry);
    TreeLocks tree;
    check_tree(extracted, L"", expected, tree, buffer);
    if (!expected.empty()) fail(L"Restored output is missing entries from the accepted ZIP");
    // Windows may require descendant handles closed to rename a directory.
    // The root/parent identity locks remain held; see README threat boundary.
    tree.files.clear(); tree.directories.clear();
    for (const auto& lock : parent.locks) info(lock.get());
    info(staging.get()); info(extraction_directory.get()); info(restored_root.get());
    assert_absent(final_path); check_cancel();
    rename_without_replace(restored_root.get(), parent.locks.back().get(), bundle::root_name);
    incomplete.clear(); // The atomic rename is the sole success commit point.
    std::wprintf(L"Restored and verified: %ls\n", final_path.c_str());
    // No application launch; no recursive deletion. Temporary cleanup is best
    // effort after success and only targets paths created by this invocation.
    verified_zip.reset();
    Handle cleanup(CreateFileW(native_path(temporary_zip).c_str(), DELETE | FILE_READ_ATTRIBUTES, 0, nullptr, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr));
    if (cleanup.valid()) { try {
        const auto current = info(cleanup.get());
        if (current.dwVolumeSerialNumber == owned_zip_identity.dwVolumeSerialNumber &&
            current.nFileIndexHigh == owned_zip_identity.nFileIndexHigh && current.nFileIndexLow == owned_zip_identity.nFileIndexLow) remove_owned_file(cleanup.get());
    } catch (const Failure&) {} }
    cleanup.reset(); extraction_directory.reset(); RemoveDirectoryW(native_path(extracted).c_str());
    staging.reset(); RemoveDirectoryW(native_path(temporary_zip.substr(0, temporary_zip.rfind(L'\\'))).c_str());
}
} // namespace

int wmain(int argc, wchar_t** argv) {
    bool interactive = argc == 1;
    std::wstring incomplete;
    try {
        if (!SetConsoleCtrlHandler(console_control, TRUE)) winfail(L"Cannot register interruption handler");
        std::wstring source = executable_parent(), destination = source;
        for (int index = 1; index < argc; ++index) {
            const std::wstring option(argv[index]);
            if ((option == L"--source" || option == L"--destination-parent") && index + 1 < argc) {
                (option == L"--source" ? source : destination) = argv[++index];
            } else if (option == L"--non-interactive") { interactive = false;
            } else if (option == L"--help") {
                std::wprintf(L"Restore this exact accepted ZIP from its adjacent numbered volumes.\nUsage: restorer.exe [--source C:\\folder] [--destination-parent D:\\folder] [--non-interactive]\nNo installation, download, elevation or application launch. Existing destinations are never overwritten.\n"); return 0;
            } else { fail(L"Unknown/incomplete option; see --help"); }
        }
        std::wprintf(L"Restoring %ls\nThis transports the original ZIP bytes; it does not reduce package size.\n", bundle::archive_name);
        restore(source, destination, incomplete);
        if (interactive) MessageBoxW(nullptr, L"The package was restored and verified. Open the restored folder to launch the application. No application was started automatically.", L"Video Subtitle Extractor", MB_OK | MB_ICONINFORMATION);
        return 0;
    } catch (const Failure& error) {
        std::wstring message = error.message;
        if (!incomplete.empty()) message += L"\nIncomplete private files were left for inspection (not a usable package):\n" + incomplete;
        std::fwprintf(stderr, L"Restoration failed: %ls\n", message.c_str());
        if (interactive) MessageBoxW(nullptr, message.c_str(), L"Restoration failed — nothing overwritten", MB_OK | MB_ICONERROR);
    } catch (const std::exception&) {
        std::fwprintf(stderr, L"Restoration failed: allocation or size error. No success should be assumed.\n");
        if (interactive) MessageBoxW(nullptr, L"Restoration failed. No success should be assumed. Incomplete staging files may remain; no existing directory was overwritten.", L"Restoration failed", MB_OK | MB_ICONERROR);
    }
    return 1;
}
