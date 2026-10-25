# Windows CPU verification: versions, evidence and limits

Runner availability and upstream requirements reviewed **2026-10-25**. A configured
job is **not** a passing result. Use the completed run for the exact commit, runner
image, dependency versions and package profile; never carry a previous run's pass
forward to a new build.

The selected Windows verification scope is **Server CI only**. Windows client,
ARM/emulation and physical GPU execution are outside this chosen coverage, not
unfinished gates awaiting permission to use someone's computer. No Windows
computer access is requested or used for this verification.

## Parallel matrix

| Actual target | Standard runner | Paddle CPU | Native OCR CPU | Meaning |
| --- | --- | --- | --- | --- |
| Windows Server 2022 x64 | `windows-2022` | Full source + extracted-package gates | Worker + source gates; customer package requires VC license acknowledgment | Configured, results come from each completed run |
| Windows Server 2025 x64 | `windows-2025` | Same independent gates | Same independent gates/license boundary | Configured, not inferred from Server 2022 |
| Windows Server 2016/2019 x64 | No standard label in this matrix | Outside selected CI matrix | Outside selected CI matrix | Do not infer backward compatibility from newer servers |
| Windows 10 x64 client, 1809+ | No standard client x64 label | Outside selected CI-only coverage | Outside selected CI-only coverage | Qt's upstream OS support is not this app's test result |
| Windows 11 x64 client | No standard client x64 label | Outside selected CI-only coverage | Outside selected CI-only coverage | Server 2025's kernel build does not make it Windows 11 |
| Windows 11 ARM64, running x64 binaries | `windows-11-arm` exists | Outside selected CI-only coverage | Outside selected CI-only coverage | Not scheduled in this matrix; never native ARM64 coverage |
| Windows native ARM64 package | Runner exists, complete package target does not | Unsupported current package target | Unsupported current package target | No pinned complete ARM64 package/runtime closure |
| Windows 7/8/8.1, Windows 10 before 1809, 32-bit Windows | Not scheduled | Unsupported current GUI/package target | Unsupported current GUI/package target | Below Qt 6.11's desktop Windows floor or wrong architecture |
| CUDA/DirectML/other GPU profiles | Outside selected CPU CI matrix | No verified GPU execution | Native CPU worker must refuse GPU | A CPU fallback/refusal is never a GPU pass |

Both CPU workflows use `fail-fast: false` and `max-parallel: 4`: each has four
OS jobs (two Windows, Linux, macOS), with no new self-hosted label, paid larger
runner or OS VM installation. Actual concurrency remains subject to the account's
standard runner quota. Native jobs share one strictly verified 15-model conversion
job; a model-gate failure blocks them rather than fabricating per-OS results.

GitHub documents the [standard runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
and names the x64 images [Windows Server 2022](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md)
and [Windows Server 2025](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md).
The runtime helper measures CIM Caption, product type, build and update revision;
it rejects a wrong client/server identity. It separately records host and Python
process ISA with `IsWow64Process2`, rejecting emulation in these native-x64 jobs.

`release.yml` still builds **one canonical Windows x64 release asset on Server
2022** after its reusable CI has checked both Windows versions. Duplicating that
release package matrix would produce identical asset names and ambiguous merge/
publication inputs. The parallel verification jobs build and verify their own
independent archives; no tag or release is created by these checks.

## What each Windows line checks

1. Actual OS edition/build, runner image/version, Python version/bitness and host/
   process architecture, tied to the Actions commit/run/attempt.
2. Source regression suite: Paddle's full suite and real CPU OCR/VSF opt-ins, or
   native worker protocol/model parity, adapter integration, packaging and import
   isolation. Read JUnit for individual skips; a suite pass does not erase them.
3. Real `QT_QPA_PLATFORM=windows` GUI launch/screenshots and light/dark/system
   preference persistence, restart and Qt signal flows. The system-theme tests
   supply **controlled system input** to real widgets; they do not toggle actual
   Windows Settings. Physical display/driver/DPI interaction remains unverified.
4. CLI help, ordinary video-to-SRT text/timestamp/non-overlap checks, and separate
   multiline, AV1 decode/fallback and timeline end-to-end regression fixtures.
5. Build the self-contained CPU archive, extract it and rerun the bundled GUI and
   ordinary/issue fixtures using its own interpreter. Verify recursive required
   PE imports and actual loaded modules/DLLs; remove host Python from PATH.
6. Exercise the actual `Start-VSE.bat --verify` launcher twice more: once with a
   clean process PATH and once with simulated Python/Conda/venv/Qt/native-library
   environment contamination. Both run the full packaged GUI/video/SRT/isolation
   checks. No system Python/Conda installation is uninstalled or modified. The
   final bundled interpreter records its own measured Windows/architecture data
   separately from setup-python's source identity.
7. Preserve evidence even after failures, and write a stage-by-stage report with
   `passed`, `failed`, `cancelled`, `not-run` or `unknown`. Only **all** required
   successful stages plus validated identity produce an execution pass.

The two additional launcher runs have a 35-minute step limit. Total native-platform
job limits are 75 minutes for Paddle and 90 for native (including worker compilation).
Linux and macOS also have mandatory final-ZIP clean-PATH/contaminated-host gates:
Linux uses Xvfb and the real XCB backend; macOS uses Cocoa, full `.command`
verification and the `.app` executable's audited CLI path. All three platform gates
require earlier steps to succeed; they never run past a failed build/precision gate
and manufacture a pass. A timeout is a failed/incomplete gate, never a pass.

The native Windows VC redistribution gate remains hard: runner-only worker/source
tests are useful evidence but not permission to ship Microsoft's redistributables.
Without existing authorized acknowledgment, package build/verify and candidate
upload stay blocked. Nothing in this matrix supplies or changes that approval.

Hosted-image PATH/DLL isolation is **not** proof of a clean Windows 10/11 client
installation with no developer runtime. Final client VM/physical-machine testing,
SmartScreen/signature behavior, real desktop theme changes and physical GPU
inference are outside verified Server CI-only coverage, not outstanding requests
for machine authorization. No network/security policy is changed here.

## Upstream requirements versus tested support

- The repository pins portable **CPython 3.12.15**. [Python 3.12's Windows guide](https://docs.python.org/3.12/using/windows.html)
  and [python-build-standalone's runtime requirements](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst)
  give Windows 8.1 as the interpreter floor. That does **not** lower the app floor.
- The PySide requirement is currently `~=6.11` (`>=6.11,<7`, not an exact patch
  pin). [Qt for Python's Qt 6.11 Windows table](https://doc.qt.io/qtforpython-6.11/overviews/qtdoc-windows.html)
  lists Windows 10 **1809+** and Windows 11 x64 (and Qt ARM64 separately). This is
  a necessary GUI baseline, not certification of every app dependency or edition.
  Reports record resolved PySide/Essentials/Shiboken versions; re-check upstream
  requirements when resolution changes, rather than assuming a permanent floor.
- Native uses the pinned ONNX Runtime SDK from `native/ocr/ort-sdk-manifest.json`;
  Paddle uses the pinned framework in `requirements*.txt`. ONNX Runtime's
  [general compatibility page](https://onnxruntime.ai/docs/reference/compatibility.html)
  is not a promise for every exact SDK/dependency/Windows combination. The full
  application has not established an oldest-working client Windows build.
- Paddle's [official Windows installation source](https://github.com/PaddlePaddle/docs/blob/develop/docs/install/pip/windows-pip_en.md)
  requires 64-bit x86_64 Python and documents AVX/MKL wheels (no `noavx` package).
  Its current installation example names 3.3.0, whereas this repo pins 3.3.1:
  it is architecture/ISA guidance, not evidence for the exact pinned binary or
  for a particular oldest Windows client version. Non-AVX Paddle machines are
  outside this matrix; ARM x64 emulation must establish the required ISA too.
- Microsoft documents [Windows 11 x64 emulation on ARM](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation).
  That feature alone is not evidence that this exact Paddle/ORT/VSF/Qt bundle and
  its CPU instruction requirements work. The current builder/launcher/package
  manifests target Windows x64, not native ARM64. No emulation lane is requested.
  If that scope is separately changed in future, such a lane must
  explicitly provision an **x64 process**, record ARM64 host versus x64 process,
  verify the unmodified final x64 archive and its ISA/import requirements, and
  label its result **x64 emulation**, never Windows 11 x64-hardware/native ARM64.

## Finding the evidence

- Paddle source: `paddle-test-results-windows-2022-X64` or
  `paddle-test-results-windows-2025-X64`; extracted package:
  `paddle-inference-package-results-<runner>-X64`.
- Native: `native-profile-evidence-<runner>-x64`;
  only a successfully gated candidate: `native-profile-candidate-<runner>-x64`.
- `windows/identity.json`, `windows/coverage.json`, `windows/coverage.md` and the
  Actions step summary record measured identity and stage results. Source version
  metadata is labeled source-only; final bundled provenance and resolved runtime
  audits live with the package evidence/`bundle-manifest.json`.
- `windows/portable-environment/portable-environment-report.json` and per-archive,
  per-scenario `environment-report.json`, dependency-isolation summaries and
  GUI/E2E outputs keep clean-PATH and simulated contaminated-host results separate.
  Linux/macOS use `linux/portable-environment/` and `macos/portable-environment/`
  inside their own OS/profile-specific artifacts, never the Windows directories.
- Source JUnit, GUI screenshots, `e2e` and `issues` reports remain separate from
  `dist-*/test-results/<archive>/` extracted-package evidence, including dependency
  isolation, GUI, `bundle-e2e`, `bundle-issues` and runtime size reports.

Artifact names include profile, OS label and architecture, so Server 2022 and
Server 2025 cannot overwrite each other's evidence/candidates. Identically named
ZIPs inside different job artifacts must remain separate when downloaded.
