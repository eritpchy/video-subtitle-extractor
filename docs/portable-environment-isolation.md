# Portable launcher environment isolation

The supported entry points are `Start-VSE.bat`, `Start-VSE.sh`, and on macOS
`Start-VSE.command` / the `.app` executable. They always select the included
interpreter by an absolute bundle-relative path. A pre-existing system Python,
activated Conda environment or venv is not a prerequisite and is not removed.

## Startup and preferences

The launcher starts Python with `-I -B -X utf8`: inherited `PYTHONHOME`, `PYTHONPATH`,
Python startup options and user-site packages cannot affect interpreter startup
**before bootstrap runs**. UTF-8 is explicit rather than inherited: `-I` ignores
CI/user `PYTHONUTF8`, so bootstrap sets its own `PYTHONUTF8=1` for children and
drops incoming `PYTHONIOENCODING`. Redirected multilingual output is tested.
Search variables for Qt/QML, OpenCV plugin/OpenCL library paths and native libraries are
cleared before the bundled interpreter starts. Linux selects only the bundle's
`native-libs` for `LD_LIBRARY_PATH`. Windows switches away from the caller's
working directory before Python starts. PATH is rebuilt from bundle interpreter
locations and explicit base-OS directories, never inherited Conda/venv/vendor
paths. No shell lookup selects a Python executable.

Bootstrap removes residual Python/Conda/venv selectors from descendant process
environments and applies the packaged manifest's OCR backend/native-worker root.
It pins `VSE_VSF_BINARY`/`VSE_VSF_DIR` and `IMAGEIO_FFMPEG_EXE` to checksum-verified
manifest entries. Missing or ambiguous FFmpeg inventories fail instead of
calling a finder that can fall back to Conda/system binaries. The reviewed VSF
`.run` wrapper may select its inventoried sibling executable; no other install
or architecture is searched. Source-mode manual executable overrides are
unchanged.

It does **not** erase unrelated application preferences: `VSE_CONFIG_FILE`,
`VSE_WORK_DIR`, HOME/XDG/LOCALAPPDATA, display/session and locale settings, Qt
scaling/platform choices and explicit native provider requests remain intact.
An explicitly unsupported GPU provider is still rejected, not silently changed.
Bootstrap options precede `--cli`; options after `--cli`, including `--help`, are
forwarded to the subtitle CLI.

This does not defend against a malicious/broken operating system, loader or
`LD_PRELOAD` code that prevents `/bin/sh` itself from starting. The tests do not
load foreign native code and do not modify system installations or global
settings. Base-OS utilities, GPU/display drivers and documented platform/ABI
requirements still apply.

## Windows system-Python registry isolation

After build-side pip installation, the packager writes `runtime/python312._pth`
with only relative bundle paths (standard library, DLLs, application root and
site-packages). This suppresses CPython's registry/PYTHONPATH expansion for
**ordinary child interpreters too**, not merely the first `-I` process. The
bundle-owned `sitecustomize.py` is created before the site-packages prune
inventory; both files are hashed by the outer release manifest. Existing
publisher/third-party path files or startup hooks are never overwritten. Normal
Windows bootstrap verifies that both required configuration files remain intact.
The relevant [CPython 3.12 path initialization](https://github.com/python/cpython/blob/v3.12.15/Modules/getpath.py)
consults PythonPath registry subkeys only while environment-based path discovery
is enabled; `._pth` disables that expansion.

The hook never imports environment-specified external code. Once bootstrap has
validated the explicit audit request, descendant hooks load the probe from the
bundle. A hook failure terminates the process instead of being swallowed by
Python's `site` module. Initial launch clears inherited audit activation values.
UTF-8 and user-site preconfiguration are still controlled separately: `._pth`
alone is not substituted for the launcher's `-I -X utf8` or sanitized child env.
The hook carries the already-active UTF-8 option into multiprocessing spawn,
whose automatically inherited `-I` would otherwise ignore `PYTHONUTF8`.

Windows package gates require **every recorded Python process** to have isolated
startup, environment-path discovery disabled, user-site disabled, and only
bundle-contained `sys.path` entries, in addition to the unchanged loaded-module
and native-library audit. The verifier may read existing HKCU/HKLM Python 3.12
PythonPath entries for evidence; it never writes/injects registry entries. Empty
or inaccessible inventory is recorded honestly, not treated as a tested registry
injection. Windows execution coverage remains the authorized Server CI matrix;
no client computer or registry-write fixture is required or implicitly authorized.

## Acceptance of the final ZIP

Run on each package's **native target OS and architecture**:

```sh
python scripts/verify_portable_environment.py --archives dist-ci --output test-results/portable-environment
# Or --archive path/to/one-final.zip (with its .zip.sha256 sidecar).
```

The verifier requires fresh evidence, verifies archive/payload hashes and runs
both cases through the actual launchers:

- `clean-host-path`: the incoming child PATH contains no host interpreter/tools.
  This is not a claim that the host has no installed Python/Qt. The launcher's
  explicit base-OS PATH is visible in the report. The separate stock-Ubuntu
  clean-container gate establishes the actual no-host-Python/Qt condition and
  also invokes `Start-VSE.sh` for full acceptance.
- `contaminated-host-environment`: temporary fake PATH commands, foreign Python
  packages and `sitecustomize`/`usercustomize` canaries, user-site directories,
  invalid Python home, simulated Conda/venv selectors, and Qt/native search
  directories, foreign VSF/FFmpeg marker programs, and deliberately ASCII-only
  Python I/O settings. Native-file markers are inert text, not executable DLLs. This is
  **simulated contamination**, not evidence of testing installed Conda versions.

Both cases run the complete bootstrap verification: dependency checks, CLI,
GUI screenshots, ordinary OCR/video-to-SRT, multiline, AV1 and timeline
regressions. On macOS the `.command` runs full verification; the `.app` executable
also launches the CLI through an independently audited process. This is not a
Finder/LaunchServices, Gatekeeper or signing test. A failing case is retained and
other cases/archives are still attempted; failures never become a pass/skip.

The existing `package_release.py --verify ... --smoke` also uses the real
launcher. Isolation does not silently disable runtime audits: the first
isolated process loads its probe from the checksum-verified bundle. Only an
exact byte-validated copy of that probe and its fixed `sitecustomize` may be
restored to child PYTHONPATH. All other external Python modules and non-base
native libraries remain forbidden by the existing strict audit. Required GUI,
OpenCV and profile-specific modules must actually be observed. External media
selector paths are also rejected; no external-program fallback counts as success. Missing reports,
missing workloads, changed hooks or surviving foreign paths fail closed.

Evidence includes the aggregate `portable-environment-report.json`, per-archive
and per-scenario reports, `environment-report.json`, `fixture.json`, launcher
logs, `dependency-isolation/summary.json`, per-process module/native-library
snapshots, screenshots and OCR/video regression results. On Windows CI,
`VSE_WINDOWS_RUNNER_LABEL` additionally records actual packaged-process OS/ISA
metadata at each scenario's `bundled-windows-identity/identity.json`.

Unit tests use explicit bridges to a real host Python for startup-flag checks;
they are not self-contained-package acceptance. POSIX-shell executions of the
macOS launcher on Linux are not native macOS execution. Native Windows/macOS,
final ZIP and clean-OS results are reported only after their corresponding jobs
actually run; merely adding this gate does not certify those environments.
