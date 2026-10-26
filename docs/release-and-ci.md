# Self-contained package candidates and CI

The current automatic build-only profile matrix, historical CUDA migration,
final-package gates and publication boundaries are documented in
[the release matrix](RELEASE_MATRIX.md). This document describes build and local
usage; it does not authorize a release.

## What the workflows actually do

- The candidate workflow retains Native CPU for Linux x64, macOS arm64 and
  Windows x64, and restores Windows Paddle CPU, CUDA 11.8, CUDA 12.6 and DirectML.
  Windows runs on Server 2022 and Server 2025; the canonical seven-package set
  uses Server 2022 artifacts while keeping both servers' acceptance evidence.
- The separate standard workflow exercises Paddle CPU on Linux x64, macOS
  arm64 and both Windows servers. Docker remains a headless Linux amd64 CPU
  check, not an automatically published registry image.
- Paddle consumers reuse all 15 original models from a fixed-hash accepted
  CPU ZIP through a verified cache and same-run artifact. They do not repeat
  independent model-origin requests. See the
  [offline seed contract and expiry limits](audits/paddle-model-seed.md).
  DirectML reuses the same-run exact-SHA converted Native model artifact.
- VideoSubFinder remains pinned to 6.10.7. Packages contain their interpreter,
  inference runtime, media tools, GUI and complete supported language assets.
  No first-run package or model installation occurs.
- Final ZIPs must pass their actual launchers, all three explicit CPU modes,
  media regressions, full model checks where applicable, and clean-PATH plus
  contaminated-host isolation. Checksums and per-file inventory are rechecked
  after execution, including prebuilt bytecode.

**A workflow definition is not a test result.** Read the completed run for the
exact source commit. Failed, skipped, cancelled and still-running jobs are not
passes. Hosted CPU acceptance does not prove CUDA/DirectML execution on a GPU,
or Windows 10/11, Windows ARM/emulation, Intel macOS or Linux ARM compatibility.
See the [Windows verification matrix](windows-verification.md) and
[portable environment isolation](portable-environment-isolation.md).

## Build-only review

Pushes and pull requests run with read-only repository permissions. Reports,
candidate ZIPs and hash-bound acceptance receipts are Actions artifacts, not
release assets. The workflow cannot create/move tags, overwrite published assets
or change VERSION. A default-branch manual-dispatch limitation is not permission
to add publication triggers.

1. Apply the reviewed changes and run the automatic candidate workflow on the
   exact intended commit.
2. Inspect the Native, Windows Paddle and DirectML jobs and the aggregate
   candidate-set result, including both real server versions.
3. Preserve candidate ZIPs, checksums, receipts and failed-stage evidence.
   Downloaded bytes must match their recorded SHA256 before reuse.
4. Coordinate a separate, explicitly approved version and publication decision.
   Already-published 2.3.0 and earlier tags/assets are immutable. Do not label
   newly built code as the old published commit.

The obsolete manual-only CPU publication instructions no longer describe these
workflows and must not be used as an alternate release route.

## Customer launchers

Customers do not install Python, pip, Paddle, ONNX Runtime, CUDA Toolkit, cuDNN
or models. An appropriate supported OS, graphical environment and GPU driver
remain platform prerequisites. Keep the extracted directory intact:

- Windows: double-click Start-VSE.bat.
- macOS: open Video Subtitle Extractor.app.
- Linux: run ./Start-VSE.sh on a compatible graphical desktop.
- CLI/verification: pass --cli ... or --verify to the included launcher.

The original ZIP remains an offline repair source. SHA256 checks corruption,
not publisher identity; use the intended authenticated artifact or approved
release. CUDA ZIP volume transport is a separate native-restorer candidate,
not a compression claim or an already-certified customer delivery path.

macOS minimum system declarations must come from the highest actual deployment
requirement across the bundled Mach-O images, not a wheel filename or the ORT
SDK alone. Do not infer full-package support for macOS 14.0 from an individual
14.0 library. The reviewed Qt 6.12.0 full payload actually reaches **26.0** in
28 original PySide/Shiboken bindings (independently confirmed arm64 `otool`
`minos`, not SDK). New Mac candidate CI therefore uses `macos-26`, and the
generated app declaration records the scan's actual result. Earlier published
14+ claims are corrected in new notes, never by replacing old ZIPs. This
metadata bound is separate from tested OS coverage.

Linux bundles must contain their non-base GUI/native dependency closure and pass
native X11 checks; build-runner apt installations do not prove that an
unprepared customer's machine works.

## Local build-side checks

Use an already verified model-only seed rather than retrying a rejected source:

    python scripts/paddle_model_seed_ci.py --verify-seed build/paddle-seed

Point source inference at its models/ directory with VSE_MODEL_DIR. Building
still requires the target platform's reviewed build dependencies and wheelhouse;
none of those build tools are customer prerequisites:

    python scripts/package_release.py --paddle-models build/paddle-seed/models --wheelhouse build/wheelhouse --output dist
    python scripts/package_release.py --verify dist --smoke

Use a clean wheelhouse per platform/profile; duplicate versions are rejected.
CUDA uses only its selected official GPU wheel, never a co-installed CPU wheel.
The old backend/tools/makedist.py QPT helper is not the release path.

### Inference-only runtime and size accounting

`requirements.txt` remains the development environment. The builder resolves
`requirements-inference.txt` against native official wheels, installs them into
the pinned portable interpreter, then excludes every wheel/cache from the final
archive. Official wheels are not repacked. The shipped derived installation
retains licenses and original `RECORD` metadata, with an explicit per-file
`runtime/inference-prune.json` explaining removals and hashing retained files.
Reviewed test assets, developer headers/type metadata and generated bytecode are
omitted; supported launchers disable bytecode regeneration. Only the tested Linux
x64 Paddle 3.3.1 `engine=paddle` profile omits its unused OpenVINO CPU plugin;
hard-linked OpenVINO core, MKL, oneDNN and Paddle inference libraries remain.

Two exact metadata edges deliberately substitute narrower official providers:

- Fluent Widgets → PySide6 uses matching Essentials/Shiboken instead of the broad
  Addons/WebEngine/PDF meta-package.
- macOS Frameless Window → PyObjC uses same-version Core/Cocoa/Quartz, with actual
  native imports checked, instead of all macOS SDK wrappers.


All other active requirements, OCR extras and provider version constraints must
pass the strict runtime audit. This is not a claim that ordinary `pip check`
accepts the intentionally narrower metadata substitutions. Native GUI/theme,
multilingual OCR, AV1, multiline subtitles and timeline tests gate the profile.
All existing bundled language-model coverage is retained; only model directories
unreachable from production selection and non-runtime marketing media are removed.

`*.zip.size.json` records compressed download and complete extracted bytes.
`test-results/runtime-size.json` records the actual included runtime, models and
total footprint per native platform. Download size is not installed size; neither
requires an additional package-installation expansion on the client. CI also
preserves the full pruning audit manifest. Docker uses the same audited inference
profile in a multi-stage build and retains no builder wheel/cache layers.
