# Self-contained native CPU / Mac CoreML package candidates

The existing Paddle profile remains the default. `--profile native` is a separate
candidate, **not a CUDA package**. macOS uses `--accelerator coreml` and the
`native-coreml` identity, with explicit CPU in the same environment. Unsupported
provider requests fail instead
of silently falling back. The adapter's correctness and native-platform release
gates must pass before promoting this candidate.

## Build contract

1. Build the reviewed worker on each target OS and prepare the 15 exact FP32 ONNX
   assets using `native/ocr/` (conversion is build-side only). Do not substitute
   an arbitrary model-hub export. The artifact must include `size-audit.json`
   containing every runtime file's path, byte count and SHA-256, a 15-entry
   `model-manifest.json` with model/config hashes, source/dependency provenance,
   native executable, target ORT library and license notices.
2. Resolve/download wheels from `requirements-native-inference.txt` on the native
   OS into a clean wheelhouse. Fluent Widgets' PySide6 meta dependency is replaced
   with real, version-compatible Essentials by `runtime_dependencies.py`; macOS
   Frameless uses Core/Cocoa/Quartz rather than the entire PyObjC umbrella.
   The checked installed closure is 27 distributions on the Linux prototype.
3. Install and verify the pinned VSF release. Commit all source changes so the
   exact source revision in the package is truthful. Build:

   ```sh
   python scripts/package_release.py --wheelhouse build/wheelhouse \
     --profile native --native-ocr build/native-ocr --output dist-native
   python scripts/package_release.py --verify dist-native --smoke
   ```

The existing Linux native dependency closure, Windows PE and macOS Mach-O audit
also cover the new worker. Ubuntu clean-container verification remains mandatory;
source tests or a cloud-host prototype do not replace it. No unknown-host C++
library bytes may be redistributed to make a diagnostic prototype pass.

## What is and is not shipped

- Included: pinned portable CPython for the GUI, minimal GUI/media Python closure,
  native worker, the unchanged pinned ORT 1.31.0 shared library (CoreML-capable
  on macOS), all 15 ONNX models/configs, VSF and FFmpeg,
  licenses/provenance, platform-attributable native dependencies.
- Excluded: Paddle/PaddleOCR/PaddleX/Python ONNX Runtime, old `backend/models`
  Paddle weights, build compiler/conversion environments, wheelhouse and pip.
  The independent `scripts/native_diagnostics` frontend is a separate build-side
  artifact, never part of the customer payload.
- Linux ORT SONAME is materialized once, not duplicated under both version names.
- Launchers configure the native backend automatically from the package manifest;
  no client Python, dependency installation, model download or host venv required.
- The `.app` payload remains movable; configuration/cache/work/evidence stay in
  writable user directories. Keep system-theme, rounded-icon and screenshot gates.

`*.zip.size.json` records the **whole application** compressed and expanded sizes.
The approximately 298 MB worker/model artifact is not the GUI application size.
A build-side inventory marked `release_ready: false` is diagnostic provenance,
not certification; the final target-platform runtime gates still decide readiness.

## Native CI and platform limits

The dedicated `native-ocr.yml` builds models once on Linux and then compiles and
verifies Linux x64, Windows Server 2022/2025 x64 and macOS arm64 independently.
The [Windows matrix](windows-verification.md) distinguishes measured server
coverage from Windows 10/11 client and ARM/emulation execution outside the chosen
Server CI-only scope. It uploads test
candidates only, never tags or releases. All adapter parity fixture variables are
set so legacy/detector/Kannada checks cannot silently skip for missing evidence.
The ORT SDK's own 14.0 target is not the full application's minimum.
Both Native and Paddle macOS bundles now scan every runtime-loadable Mach-O
slice after staging, write the highest declared requirement to Info.plist, and
bind the image/hash report in the bundle manifest. Final ZIP verification
rescans the extracted payload and rejects a stale or understated declaration.
The official Qt 6.12.0 Cocoa image declares 14.4, but the complete payload's
PySide/Shiboken bindings declare **26.0**, confirmed independently with Apple's
`otool` on the arm64 QtCore image. Thus the current complete application requires
macOS 26.0+, not 14.0 or 14.4. Wheel filename tags and direct interpreter startup
on macOS 15 must not be used to claim lower app-launcher support. The new CI
candidate runs on explicit `macos-26`; the scanner's metadata bound remains
separate from execution on the oldest permitted minor OS version. Published
historical archives are unchanged; their earlier 14+ claim is not repeated.

Windows redistribution is not silently assumed: the builder requires an explicit
acknowledgment of applicable Microsoft Visual C++ redistributable rights. The
manual workflow input `acknowledge_vc_redistribution_license`, or an already
approved repository variable `VSE_VC_REDISTRIBUTION_LICENSE_ACK=true`, supplies
that acknowledgment. Without it Windows still compiles and performs runner-only
tests using the runner's existing runtime, but does not copy redistributable VC
DLLs. An explicit pending/failing customer-package gate then prevents packaging
and candidate upload. Linux/macOS full checks continue normally. Do not set the variable on the user's behalf without their
confirmation. Official pinned runtime/notice extraction is not itself proof of
license eligibility.

## Explicit Mac provider contract

Build with `--profile native --accelerator coreml --native-ocr build/native-ocr`.
The native artifact must include its actual bounded `--capabilities` result,
inventoried and tied to exact worker/runtime hashes. Availability alone is not
execution proof. Staging also independently verifies the fixed 15-model pins,
not just artifact-supplied hashes. Bootstrap selects the manifest's CoreML
policy; CLI `--cpu` and the GUI hardware switch explicitly select CPU without
changing the environment, model set or VSF path.

Final package gates execute both providers across 168 cases each and real
three-mode video flows. Raw ORT EP evidence remains separate from physical GPU
and internal precision evidence; the production release gate stays blocked
until the actual Mac requirements, including GUI accessibility, are verified.
See [the complete matrix/acceptance contract](RELEASE_MATRIX.md).
