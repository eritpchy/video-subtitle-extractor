# Reproducible experimental native OCR profile

Default VSE remains Paddle. This directory is source, pinned build recipes and
small parity fixtures—not model weights, environments or compiled libraries.
The C++ worker performs full image detection/crop/CTC inference using exact FP32
Paddle-derived models. Build-time Python is never copied into the worker runtime.

## Build contract

Python 3.12 is required for the model/source build helpers. Use isolated build
virtual environments; do not install conversion tooling into the GUI runtime.

```sh
# One Linux conversion job, reusable artifacts for all native platforms:
python -m pip install -r native/ocr/requirements-model-build.txt
python native/ocr/prepare_models.py --output build/native-models --cache build/model-cache

# Native OS/architecture job (Linux x64, Windows x64, macOS arm64):
python -m pip install --only-binary=:all: --require-hashes -r native/ocr/requirements-build.txt
python native/ocr/build.py --platform linux --arch x64 \
  --models build/native-models --output build/native-ocr \
  --work-dir build/native-build --cache build/native-cache
python native/ocr/smoke.py --artifact build/native-ocr --output test-results/native-worker.json
```

`--offline` on either build helper rejects cache misses without network fallback.
Existing output artifacts are never overwritten. `--cmake`, `--jobs` and optional
`--generator` select installed build tools; no helper invokes pip automatically.
Windows requires explicit `--generator Ninja` and the verified x64 MSVC developer
environment described below; it must not depend on CMake's changing default
generator. macOS always targets arm64/macOS 14.0.

### Windows Server CI toolchain (source-side gate)

Both `windows-2022` and `windows-2025` remain independent parallel jobs. After
installing the hash-pinned build requirements, the workflow runs:

```powershell
python native/ocr/windows_toolchain.py --report test-results/windows/native-toolchain.json --github-env "$env:GITHUB_ENV"
# The following CI step receives the selected developer environment:
python native/ocr/build.py --platform windows --arch x64 --generator Ninja --models build/native-models --output build/native-ocr --runner-test-only
```

`vswhere` discovers installed C++ toolchains rather than assuming a Visual Studio
year or installation path. The helper selects an installed v143/14.44 x64 toolset
whose own `include/crtversion.h` CRT version is no newer than the committed
`vc-runtime-manifest.json` **14.51.36247.0** runtime. It initializes that exact
toolset with `VsDevCmd.bat -arch=x64 -host_arch=x64 -vcvars_ver=<version>`, verifies
compiler/linker and INCLUDE/LIB precedence against that exact preselected
directory/header/import-library snapshot, and keeps pinned CMake **4.1.2** and
Ninja **1.13.0** ahead of Visual Studio's bundled tools. The compiler's 19.44
servicing patch is recorded separately: it is **not** mechanically converted
into a CRT minimum version. A newer toolset family, missing/ambiguous CRT macros,
or newer CRT header version fails before compilation, retaining candidate paths,
versions and the reason; there is no fallback to `latest`, automatic installation,
runtime pin change, license acknowledgment or security-setting change.

Initialization uses an explicitly quoted `cmd.exe` command-line string, not
Python's C-runtime argv quoting (which inserts inappropriate `\"` around a batch
path). It retains the documented full version selector; there is no prefix or
cross-instance fallback. The report preserves the initialization command, script
hash, return code and bounded diagnostics, but never the raw `set` environment
dump. A delimiter separates diagnostics from allowlisted environment parsing;
assignment lines and known inherited secret values are omitted from diagnostics.
Windows CI runs the command regression tests **before** toolchain initialization,
including real `cmd.exe` fixtures with spaces, ampersands and parentheses in the
batch path, successful initialization and nonzero diagnostic preservation.

OpenCV's static libraries explicitly use `BUILD_WITH_STATIC_CRT=OFF` and
`CMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL`; the worker also explicitly uses
`MultiThreadedDLL`. This matters because OpenCV's own static-CRT default can
override the CMake runtime setting. See the pinned
[OpenCV CRT selection code](https://github.com/opencv/opencv/blob/4.10.0/cmake/OpenCVCRTLinkage.cmake).

Official rolling-image manifests checked on **2026-10-25** advertise:

| Runner | Image version | VS Enterprise | Windows SDK | Installed release VC runtime |
| --- | --- | --- | --- | --- |
| [Server 2022](https://github.com/actions/runner-images/blob/main/images/windows/Windows2022-Readme.md) | 20261022.344.1 | 2022 / 17.14.37806.7 | Includes 10.0.26100.0 | 14.51.36247 |
| [Server 2025](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md) | 20261021.299.1 | 2022 / 17.14.37806.7 | 10.0.26100.0 | 14.51.36247 |

Those pages advertise the C++ x86/x64 component, but do **not** establish the
exact compiler directory or matching CRT header on the next allocated runner.
The per-job JSON is authoritative for that source-side check; it records the
actual image, VS/toolset, compiler/linker versions, SDK, CRT header/import-library
SHA256s and build tools, including failure evidence. Successful assembly also
records the active toolchain in `size-audit.json` provenance. The newer host CRT
is not permission to compile against it while shipping the older pin.

[Microsoft's compatibility restrictions](https://learn.microsoft.com/en-us/cpp/porting/binary-compat-2015-2017)
require a sufficiently recent runtime for all inputs. Header/import-library
evidence is a **source-side constraint**, not proof that the compiled worker,
ORT, or the whole GUI will load with bundled DLLs. Exact imported symbols,
actual loaded-module paths and application smoke/parity remain separate package
gates; DLL names alone never establish compatibility. Actual Server 2022/2025
execution of this change remains pending the next CI run, not a Linux unit-test
claim or a Windows 10/11 client certification.

The output contains `native-ocr[.exe]`, one physical copy of each required ORT
runtime library, `models/`, `config/`, licenses and complete SHA256 inventories.
Linux's primary library is materialized as `libonnxruntime.so.1`, macOS's as
`libonnxruntime.1.dylib`, matching their SONAME/install ID. No PDB/dSYM, SDK headers,
static build libraries, source archives, training/conversion tools or Python
inference modules are shipped. `scripts/package_release.py --profile native
--native-ocr build/native-ocr` consumes this inventory for the larger one-click app.
`size-audit.json` excludes its own hash (self-hashing is impossible), but includes
its size in `assembled_total_bytes`.

## Pinned inputs

- `model-source-pins.json`: all 15 official Paddle archive URLs and observed SHA256,
  exact three inference source hashes per model, deterministic ONNX and config hashes.
  All 45 source files match the previously tested project weights. There is no
  precision reduction, model identity substitution or automatic language fallback.
- Paddle2ONNX 2.1.0, ONNX opset 17/checker, explicit `--optimize_tool None`, disabled
  automatic opset changes, `POLYGRAPHY_AUTOINSTALL_DEPS=0`, `PIP_NO_INDEX=1`.
  Four V6 and three recovered models reproduce previous ONNX bytes exactly. Eight
  legacy graphs previously had Polygraphy constant folding; their new **unoptimized**
  graph hashes are intentionally distinct and both old/new hashes are recorded.
  New graph parity is verified rather than silently claiming byte identity.
- `ort-sdk-manifest.json`: official ORT 1.31.0 release SDK URLs, archive/per-file
  hashes, licenses, native dependencies and minimum OS metadata for three targets.
- `deps/source-pins.json`: OpenCV 4.10.0, Clipper 6.4.2 from official pyclipper 1.4.0
  source, nlohmann JSON 3.12.0 and notices. Cached extracted OpenCV sources are
  rechecked against the pinned archive before compilation.
- `build-tool-pins.json` / `requirements-build.txt`: CMake 4.1.2 and Ninja 1.13.0
  official PyPI wheel URLs/hashes; tools stay outside runtime.

Only OpenCV core/imgproc/imgcodecs with static PNG/JPEG/zlib are built. IPP,
OpenCL, GUI, video, Python and other optional stacks are disabled. Video/AV1
extraction remains the parent application's pinned FFmpeg responsibility.

## Protocol and parity

Binary stdin/stdout: uint32 little-endian byte length plus UTF-8 JSON, maximum
request 65,536 bytes. Request fields: `id`, `tier` (`small`/`medium`), `path`, optional
`operation` (`ocr`/`detect`), `provider` (`cpu` only), `batch_size` (1..100),
`cpu_threads` (1..128), `timeout_ms` (1..120000), and exact `rec_model` without `_infer`.
Unknown fields/providers are rejected; GPU requests never silently fall back to CPU.

OCR returns `result: [boxes, [[text, confidence], ...]]`. Detection returns top-level
raw `dt_polys` without recognition filtering or axis-aligned conversion. Crucially,
standalone `detect` uses actual V6 TextDetection **960/max, .2/.45/1.4** defaults;
OCR uses pipeline **64/min, .3/.6/1.5** overrides. Recognition batch padding affects
scores: compare to the same batch size, not blindly to default batch 6.

Arabic models explicitly return `text_order: logical`; the production adapter must
apply existing `bidi.algorithm.get_display`. Other models report `display`. `kn` and
`ka` are this project's existing Kannada aliases, not claimed Georgian support.

The host supplies `VSE_OCR_PARENT_PID` to close the pre-main parent-death window.
Linux uses PDEATHSIG, Windows a retained parent handle plus creation-time check,
macOS a parent watchdog. Windows uses wmain/Unicode filesystem paths and binary
pipes. ORT cancellation covers inference deadlines; the host still owns a whole
request deadline/kill/reap for load/decode stalls. Image byte/pixel, frame/nesting,
model path and batch memory limits fail explicitly. ORT telemetry is disabled
before initialization and by API; offline clean-container validation remains a gate.

`smoke.py` uses only Python's standard library as a build-side client: 110 frozen
parity cases across all 15 models plus malformed input/GPU refusal/recovery checks.
`tests/evidence/FIXTURES.md` documents small PNG origins and actual Paddle goldens.
No golden is regenerated from the implementation under test. These are equivalence
checks, not a language recognition-accuracy benchmark. Tests and image-operation
proofs run separately from the larger GUI/SRT E2E suite.

## Windows license-pending mode

Without established redistribution entitlement, use **only**
`--runner-test-only` on Windows. It compiles/stages the MIT ORT worker and model
inputs for tests using the runner's existing VC installation, copies **no VC DLLs**,
and marks the inventory `runner_test_only` / `vc_redistribution_pending`.
The workflow must not package or upload this as a customer candidate.

Only when the distributor has confirmed applicable Microsoft redistribution rights
may `--acknowledge-vc-redistribution-license` enable the pinned app-local VC source.
The helper statically extracts the reviewed **14.51.36247.0** official package; it
never executes the installer or copies ordinary System32 DLLs. This pin covers
the actual **14.44.35220.0** CRT headers observed in Server 2022/2025 CI; neither
the 14.44 compiler-family restriction nor the header/runtime comparison is relaxed.
Four x64 DLLs and two independently hash-pinned official license documents are
verified. The current package links to the 2026 terms instead of embedding an RTF;
the original 2015–2022 notice is also preserved, without replacing the current
terms. Downloads are content-addressed and verified even on cache hits.
`--vc-runtime-dir` + `--vc-license` can instead supply those same pinned bytes
explicitly (`license.docx` plus the historical notice and four DLLs). Merely finding Visual Studio on a runner does
not establish the repository owner's licensing qualification.

Microsoft's [redistribution guidance](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)
limits redistribution to licensed Visual Studio users under applicable terms;
[the current runtime terms](https://visualstudio.microsoft.com/license-terms/vs2026-ga-visualcpp-v14-redist-runtime/),
[the previous runtime notice](https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/)
and the applicable VS REDIST list must be retained/observed. Source hashes prove
origin, not entitlement. No CI command should automatically assert unverified rights.
The current [VS 2022 REDIST list](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)
also references the latest downloadable runtime for licensed Visual Studio users;
this is not a determination of any particular distributor's contractual rights.
The 2026 terms have changed, including use/updated-terms, feedback and trade-law
provisions. Reviewing/downloading these documents is not an explicit acceptance;
the applicable qualification must be established before activating distribution.
Python's existing Shiboken/PySide CRT source is separate and is not changed here.

## Remaining release gates

- Official Mac SDK and wheel both require **macOS 14.0**; no older baseline claim.
- The Windows SDK needs four VC DLLs; their exact SDK import symbols were verified,
  but the newly compiled worker and actual Windows load must still be audited.
- Linux GCC/libstdc++/libgcc provenance and supported OS baseline must come from
  the attributable CI runner, not this diagnostic container's unattributable libs.
- Windows/macOS native execution, actual loaded libraries, read-only relocation,
  clean no-network/no-host-Python client tests and full GUI/video/SRT tests remain
  distinct gates. `release_ready` stays false until final packaging closes them.
- Mac's SDK contains other providers, but this worker enables only CPU. CUDA,
  DirectML/CoreML hardware behavior is unverified and not implicitly enabled.

No push, tag, release or change to the default backend is performed by these helpers.
