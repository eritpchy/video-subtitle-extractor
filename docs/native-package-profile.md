# Experimental self-contained native CPU package

The existing Paddle profile remains the default. `--profile native` is a separate
candidate, **not a CUDA package**. Unsupported GPU provider requests fail instead
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
  native worker, ORT CPU shared library, all 15 ONNX models/configs, VSF and FFmpeg,
  licenses/provenance, platform-attributable native dependencies.
- Excluded: Paddle/PaddleOCR/PaddleX/Python ONNX Runtime, old `backend/models`
  Paddle weights, build compiler/conversion environments, wheelhouse and pip.
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
The official Qt 6.12.0 Cocoa image already declares 14.4; wheel filename tags
must not be used to claim full-package 14.0 support. The scanner's metadata
bound does not establish execution on the oldest permitted OS.

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
