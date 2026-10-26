#include "../protocol_output.hpp"
#include <iostream>
#include <thread>
#ifndef _WIN32
#include <sys/wait.h>
#endif

void expect(bool condition, const char *message) {
  if (!condition) throw std::runtime_error(message);
}
void raw_noise(const char *message) {
#ifdef _WIN32
  _write(_fileno(stdout), message, static_cast<unsigned int>(std::char_traits<char>::length(message)));
  const char win32[] = "win32-stdout-noise\n";
  DWORD written = 0;
  WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), win32, sizeof(win32) - 1, &written, nullptr);
#else
  const auto ignored = ::write(STDOUT_FILENO, message, std::char_traits<char>::length(message));
  (void)ignored;
#endif
}
void check_no_inheritance(const native_protocol::Output &output, const char *executable) {
#ifdef _WIN32
  DWORD flags = 0;
  expect(GetHandleInformation(reinterpret_cast<HANDLE>(_get_osfhandle(output.test_descriptor())), &flags),
         "cannot inspect private handle");
  expect(!(flags & HANDLE_FLAG_INHERIT), "private protocol handle is inheritable");
  (void)executable;
#else
  expect((fcntl(output.test_descriptor(), F_GETFD) & FD_CLOEXEC) != 0,
         "private protocol descriptor lacks close-on-exec");
  const auto descriptor = std::to_string(output.test_descriptor());
  const auto child = fork();
  expect(child >= 0, "cannot fork inheritance regression child");
  if (child == 0) {
    execl(executable, executable, "--assert-closed", descriptor.c_str(), static_cast<char *>(nullptr));
    _exit(99);
  }
  int status = 0;
  pid_t waited;
  do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
  expect(waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0,
         "private protocol descriptor survived exec");
#endif
}
int main(int argc, char **argv) {
#ifndef _WIN32
  if (argc == 3 && std::string(argv[1]) == "--assert-closed") {
    errno = 0;
    const int result = fcntl(std::stoi(argv[2]), F_GETFD);
    return result == -1 && errno == EBADF ? 0 : 9;
  }
#endif
  try {
    expect(argc == 2, "expected test mode");
    const std::string mode = argv[1];
    {
      native_protocol::Output output;
      check_no_inheritance(output, argv[0]);
      std::printf("printf-main-noise\n");
      std::cout << "cout-main-noise\n" << std::flush;
      raw_noise("raw-main-noise\n");
      std::thread background([] {
        for (int i = 0; i < 100; ++i) {
          std::printf("printf-background-noise %d\n", i);
          std::cout << "cout-background-noise\n" << std::flush;
          raw_noise("raw-background-noise\n");
        }
      });
      try {
        if (mode == "capabilities") {
          output.capabilities("{\"available\":true,\"unicode\":\"\\u5b57\"}");
        } else if (mode == "frames") {
          // 266-byte body => header begins 0x0A. Windows text translation must
          // not insert CR in this binary frame header.
          output.frame("{\"data\":\"" + std::string(255, 'x') + "\"}");
          output.frame("{\"data\":\"" + std::string(2 * 1024 * 1024, 'y') + "\"}");
          output.frame("{\"after\":true}");
        } else if (mode == "bounds") {
          int rejected = 0;
          try { output.frame(""); } catch (const std::runtime_error &) { ++rejected; }
          try { output.frame(std::string(native_protocol::Output::max_frame_bytes + 1, 'x')); }
          catch (const std::runtime_error &) { ++rejected; }
          try { output.capabilities(std::string(65536, 'x')); }
          catch (const std::runtime_error &) { ++rejected; }
          expect(rejected == 3, "protocol limits were weakened");
          output.frame("{\"rejected\":3}");
        } else {
          throw std::runtime_error("unknown test mode");
        }
      } catch (...) { background.join(); throw; }
      background.join();
      std::fflush(stdout);
    }
    std::printf("after-destruction-noise\n");
    std::cout << "cout-after-destruction-noise\n" << std::flush;
    raw_noise("raw-after-destruction-noise\n");
    return 0;
  } catch (const std::exception &error) {
    std::fprintf(stderr, "protocol output test: %s\n", error.what());
    return 1;
  }
}
