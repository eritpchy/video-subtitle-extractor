# Isolated Mac CPU / CoreML diagnostics (not a release backend)

This benchmark is **not itself a production acceleration backend**. The separately
authorized `native-coreml` rollout owns the production worker, adapter, UI and
packaging contracts. This directory does not change those contracts, models,
configuration files, goldens or tolerances. It never installs a dependency or
rebuilds ONNX Runtime. Diagnostic success does not make a production package ready.

The pinned runtime is **ORT 1.31.0**, not DirectML's independent 1.20.1. The Mac
arm64 SDK links CoreML, but a compiled-in provider is not an enabled application
path, and neither provider availability nor `CPUAndGPU` proves GPU execution.

## Three intentionally different controls

| Diagnostic label | ORT session | Allowed CoreML devices |
|---|---|---|
| `ort-cpu` | Original CPU EP/default graph settings | Not used |
| `coreml-cpu` | CoreML EP, `ModelFormat=MLProgram` | `CPUOnly` |
| `coreml-gpu-permitted` | CoreML EP, `ModelFormat=MLProgram` | `CPUAndGPU`, **ANE excluded** |

All modes use the same physical, hash-pinned runtime, the same 15 original FP32
ONNX files (2 detectors / 13 recognizers), fixed thread count and batch size.
No FP16/INT8 conversion, static-shape rewrite, OCR downgrade, substituted language
or alternate model is permitted. CoreML uses `AllowLowPrecisionAccumulationOnGPU=0`
and identical dynamic-shape/subgraph settings. This option alone does **not** prove
every intermediate is FP32. Evidence runs retain the generated MLProgram cache
for separate type inspection; `internal_coreml_precision_verified` remains false
until that inspection is actually performed. Do not substitute the default
`NeuralNetwork` format: its GPU precision behavior is different.

The C++ frontend includes `native/ocr/worker.cpp` under a renamed,
unused production entrypoint. Its own main uses the same `Engine`/preprocessing/
postprocessing. A private copy of the ORT API table clones session options before
registering a provider and records timings/profile files. It never edits a
production session's options, changes the original source or uses the production
IPC provider guard to enable GPU. Build provenance hashes both the included
worker and its provider-evidence helper. CPU diagnostic equivalence is checked against
the frozen original boxes/text/confidence tolerance (`1e-5`).

## Telemetry must be disabled *before process creation*

`benchmark.py` sets `ORT_DISABLE_TELEMETRY=1` in every diagnostic child's environment
before `exec`. The C++ entrypoint refuses to initialize without that value. It
also calls `DisableTelemetryEvents` before sessions. The API call alone is too
late for an initialization event. Do not launch the raw binary without the
environment variable or replace the policy with fake CI flags.

This is the documented full-process opt-out in the exact pinned SDK source:
[ORT privacy contract](https://github.com/microsoft/onnxruntime/blob/402979ac1bef89dcd55add41eaa7277c407599eb/docs/Privacy.md#disabling-telemetry)
and [initialization suppression](https://github.com/microsoft/onnxruntime/blob/402979ac1bef89dcd55add41eaa7277c407599eb/onnxruntime/core/platform/telemetry_1ds.cc).
It prevents uploader/event/device-ID creation; no network-permission workaround,
host-security change or approval for external metadata transmission is implied.
If execution is blocked, preserve the evidence and stop the dependent run.

## Offline build and provider probe

If only the packaged dylib needs checking, a smaller probe uses the three pinned
C API headers and the existing system C++ compiler—**no OpenCV or models**:

```sh
python scripts/native_diagnostics/benchmark.py build-probe \
  --sdk /existing/pinned-sdk-or-header-only-root --cxx /usr/bin/clang++ \
  --output /new/tiny-probe-build
python scripts/native_diagnostics/benchmark.py runtime-probe \
  --runner /new/tiny-probe-build/vse-runtime-probe \
  --library /existing/native-package/libonnxruntime.1.dylib \
  --output /new/tiny-probe-result
```

The launcher verifies the dylib's pinned SHA256 **before loading it**, sets the
process-lifetime opt-out, and verifies the loaded path/hash afterwards. This probe
calls `GetAvailableProviders`; it does not create an environment/session or run a
model. It reports capabilities of the runtime, not the production application's
selected backend. If the current compiler/tools need installation, a new license
or additional permission, stop rather than changing the computer's setup.

Use existing verified inputs. The build helper verifies SDK headers/runtime and
JSON/Clipper against committed hashes. Existing OpenCV archive hashes and its
actual build string are recorded; callers must additionally establish whether
their OpenCV build matches the release they want to compare. No automatic install
or missing-input download is attempted. This builds only the small frontend.

```sh
python scripts/native_diagnostics/benchmark.py build \
  --sdk /existing/onnxruntime-osx-arm64-1.31.0 \
  --deps /existing/verified-json-and-clipper \
  --opencv-dir /existing/opencv-install/lib/cmake/opencv4 \
  --cmake /existing/cmake --jobs 2 --output /new/diagnostic-build

python scripts/native_diagnostics/benchmark.py probe \
  --runner /new/diagnostic-build/vse-provider-diagnostic \
  --output /new/provider-probe
```

The probe checks the **actually loaded** dylib path/hash, ORT version/build and
available providers. On Mac it reads OS, CPU, RAM, power status and only asks
`xcrun --find xctrace`; it does not start tracing, change settings, elevate or open
a GUI. A Metal-capable device, installed `onnxruntime` Python wheel, `otool` link,
or `CoreMLExecutionProvider` in this list does not certify inference on the GPU.
Linux x64 validates this diagnostic; CoreML is unsupported there, never relabeled
CPU execution. Mac execution must be native arm64, not Rosetta.

## Same-host model / OCR benchmark

```sh
python scripts/native_diagnostics/benchmark.py suite \
  --runner /new/diagnostic-build/vse-provider-diagnostic \
  --artifact /existing/native-model-root --output /new/mac-provider-suite \
  --modes ort-cpu coreml-cpu coreml-gpu-permitted \
  --threads 4 --batch-size 6 --warmup 3 --repeats 10 --process-repeats 3
```

`artifact` supplies `models/` and `config/`; all 15 model hashes and all config
hashes must match the current pins. The default corpus chooses 13 existing
nonblank OCR fixtures covering all 15 models. `--cases` is a deliberately smaller
diagnostic; the report then exposes incomplete model coverage.

1. An independent **evidence-only run** captures original CPU FP32 tensor bytes,
   model/image/config/input/output hashes, output parity, verbose ORT partition
   logs, ORT kernel profiles and CoreML compute-plan logs/cache (when supported).
2. Every mode replays the **same captured tensors**, not a separately preprocessed
   approximation. This isolates model inference when detector/crop numerical
   differences might otherwise change recognition inputs.
3. Unprofiled processes measure one cold first pass, additional discarded warmups,
   then repeated synchronous passes with outputs materialized. Configuration order
   is seeded/interleaved for each process repetition. Capture/profiling I/O is not
   included in the reported warm timing samples.
4. Results include per-pass wall and ORT Run time, lazy/session creation time,
   process-entry-to-first-prediction time, per-process medians, pooled median,
   quartiles, p95, MAD, input throughput and peak process RSS. CPU/CoreML data
   transfer/copy cost inside the measured API is included, not subtracted.

"Cold" means a fresh process/session, **not** purged OS/filesystem/CoreML system
caches. Timed CoreML runs use no explicit persistent model cache; evidence caches
are isolated and never shared with timed sessions. A future cached-start study
must be a separately labeled experiment. Keep the Mac on a stable power mode,
record thermal/power conditions, avoid other substantial workloads, and do not
change those settings without authorization. Existing running GUI/rendering work
can contaminate system-wide GPU counters.

RSS is not GPU VRAM or a complete unified-memory/process-tree accounting. Missing
memory counters remain null. Numerical tensor tolerances are diagnostic only;
they do not relax the frozen OCR release gate. Failed conversion, parity, missing
CoreML kernels, unsupported nodes, CPU EP execution and all-CPU fallback must
remain visible. `diagnostic_complete` means collection ended, not release/GPU pass.

## Actual video stage measurements (explicitly not production SRT E2E)

```sh
python -m scripts.native_diagnostics.video \
  --runner /new/diagnostic-build/vse-provider-diagnostic \
  --artifact /existing/native-model-root --output /new/mac-video-diagnostic \
  --video '/existing/test video.mp4' --vsf /existing/VideoSubFinderCli \
  --ffmpeg /existing/ffmpeg --ffprobe /existing/ffprobe \
  --area 0.6,1,0,1 --tier small --rec-model PP-OCRv6_small_rec \
  --threads 4 --vsf-threads 4 --batch-size 6 --process-repeats 3
```

This runs **video → fixed VSF 6.10.7 → deterministic selected-frame decode/ROI →
OCR JSON** for every configuration and measures real sequential wall time, not a
sum manufactured from a cached preprocessing run. Timestamp lists and exact frame
hashes must match. No CUDA VSF, hardware-decoder option or automatic transcode is
enabled. FFmpeg/ffprobe are existing caller-supplied tools, hashed in the report;
their protocol allowlist is local files/pipes only.

The report separates:

* VSF wall time (**includes VSF's own decoding**, cannot claim pure selection time);
* selected-frame decode + subprocess startup + image/hash I/O;
* cold OCR subprocess, OCR pipeline and ORT Run time;
* actual diagnostic total, source-video effective fps (only when frame count is
  known), video realtime factor, and separately warmed OCR on frozen frames.

It excludes SRT text merging/post-processing and deliberately uses explicit FFmpeg
frame decoding. Therefore `production_video_end_to_end_seconds` is **null** and
the measured number is named `diagnostic_video_to_ocr_json_seconds`. The earlier
production `accurate` path switched from VSF to detection-based frame selection
when `has_accelerator()` became true. The separately authorized native-CoreML
rollout must preserve VSF for the matched CPU/CoreML comparison. Comparing different
paths without fixing the input schedule would confound algorithm and device.
Production end-to-end speed requires a further authorized, identical-path study;
do not label this diagnostic as the shipping application's GPU speedup.

## Evidence needed before calling anything "GPU acceleration"

Evidence levels must not be collapsed:

1. Runtime/provider availability and `CPUAndGPU`: **capability/permission only**.
2. ORT partition + kernel profile: proves **CoreML EP execution**, not its device.
   CPU EP node durations identify visible fallback. Fused-node counts are not an
   operator coverage percentage; CPU fallback *inside* CoreML is not visible.
3. `ProfileComputePlan`: ORT 1.31.0 prints `preferredComputeDevice` and
   `estimatedCost` from `MLComputePlan` (macOS 14.4+). This is a **plan**, not a
   measurement of the GPU actually executing the prediction.
4. An authorized, process-attributed **Core ML + GPU runtime trace** correlating
   the same model/prediction intervals with GPU work is actual GPU evidence.

The coordinator may first inspect existing `xctrace help`/templates on the Mac.
Only an already-installed, already-authorized no-GUI trace route may be used.
Never install Xcode, accept a new license, run sudo, change Developer Tools/SIP
permissions, enable privileged counters or open a permissions UI to force a
trace. If that route is unavailable, report the limitation; activity-monitor
utilization, global GPU busy%, the model's speed, and a compute plan do not replace
attributable execution evidence. A separate trace run also avoids contaminating
benchmark latency. No trace is automatically launched by normal probe/suite/video commands.

### Optional application-local Metal capture (explicit evidence command)

There is an explicit Apple public-API capture path.
It does not use Instruments, read its denied cache, attach to another process,
change system permissions, or open Xcode. On macOS it links only the existing
Foundation and Metal system frameworks; Linux builds a refusing stub. Under the
separately authorized same-binary diagnostic contract, the shared helper is also
linked into the shipping worker, but normal execution never calls it. Merely
building or launching the normal application does not capture anything.

For evidence tied to the **actual final ZIP worker**, use this build-side harness:

```sh
python -m scripts.native_diagnostics.production_capture probe \
  --native-root /existing/final-bundle/native-ocr \
  --worker-sha256 TRUSTED_FINAL_WORKER_SHA256 \
  --bundle-manifest /existing/final-bundle/bundle-manifest.json \
  --output /new/final-worker-metal-capability
python -m scripts.native_diagnostics.production_capture capture \
  --native-root /existing/final-bundle/native-ocr \
  --worker-sha256 TRUSTED_FINAL_WORKER_SHA256 \
  --bundle-manifest /existing/final-bundle/bundle-manifest.json \
  --case 0 --provider coreml --output /new/final-worker-metal-evidence
```

The harness verifies the supplied trusted executable hash, pinned runtime and all
15 models/configs before launching, rechecks them afterwards, and binds the native
payload to the final bundle manifest. Omitting `--bundle-manifest` yields only
build-artifact scope, never final-bundle verification. Only explicit commands set
`MTL_CAPTURE_ENABLED=1`; loader-injection variables are removed. The worker requires
an explicit diagnostic CLI entrypoint, a single framed fixed-fixture request and
EOF, and writes only in a new private evidence directory. PID is also observed by
the launcher, not merely copied from a response. New filesystem permissions or
GPU inspection permissions are not granted automatically.

The independent frontend below remains useful during development, but its trace
directly proves only its **different executable**, not the final package worker:

```sh
python scripts/native_diagnostics/benchmark.py metal-probe \
  --runner /new/diagnostic-build/vse-provider-diagnostic --output /new/metal-capability
python scripts/native_diagnostics/benchmark.py metal-capture \
  --runner /new/diagnostic-build/vse-provider-diagnostic \
  --artifact /existing/native-model-root --case 0 --mode coreml-gpu-permitted \
  --output /new/one-frozen-case-metal-evidence
```

`metal-probe` sets `MTL_CAPTURE_ENABLED=1` before process creation and checks
`supportsDestination(GPUTraceDocument)`; it does not capture. `metal-capture`
accepts only a frozen, hash-verified OCR fixture by index, records one cold pass,
and saves `.gputrace` only to its new evidence directory. Model/tensor bytes can
be present in that local trace. Each synchronous ORT call is associated with
model name, exact input/output hashes, PID and UTC/monotonic time windows. Stop on
any permission error; no alternate permission/cache strategy is attempted.

The selected `MTLCreateSystemDefaultDevice` object may not be the object CoreML
uses. Missing commands are therefore inconclusive, not proof of CPU execution.
Buffers created before capture starts are outside the API's capture guarantee,
which can also leave gaps when sessions initialize before the prediction interval.
Conversely, a nonempty trace or submitted command buffer is not by itself proof
of actual CoreML GPU dispatch. A reviewer must attribute relevant compute commands
and execution to this prediction, not compilation, another task, or synthetic
calibration. The report deliberately keeps device-object match, coverage and GPU
verification unconfirmed. Inspecting the trace requires existing, authorized
tools; do not install or grant permissions automatically. Capture overhead makes
**all capture-run timings ineligible for performance comparisons**. Normal probe,
suite and video processes remove `MTL_CAPTURE_ENABLED`, even if inherited.

See [Apple's programmatic capture contract](https://developer.apple.com/documentation/xcode/capturing-a-metal-workload-programmatically)
and [command-line GPU trace inspection](https://developer.apple.com/documentation/xcode/debugging-with-interactive-command-line-tools).

The reports deliberately leave `gpu_execution_verified=false` and
`gpu_speedup=null`; even a manually supplied trace must be reviewed before making
a GPU claim. Once GPU dispatch and precision are established, compare both
**CoreML CPUOnly vs CPUAndGPU** (compute-unit control) and **current ORT CPU vs the
GPU-enabled configuration** (user-relevant runtime comparison). Report CPU fallback
and mixed CPU/GPU execution rather than calling it GPU-only. ANE is never GPU.

Primary references:

* [ORT CoreML configuration](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html)
* [ORT 1.31.0 compute-plan implementation](https://github.com/microsoft/onnxruntime/blob/402979ac1bef89dcd55add41eaa7277c407599eb/onnxruntime/core/providers/coreml/model/model.mm)
* [Apple compute-unit choices](https://developer.apple.com/documentation/coreml/mlcomputeunits)
* [Apple typed execution / precision](https://apple.github.io/coremltools/docs-guides/source/typed-execution.html)
* [Apple Core ML + GPU Instruments execution analysis](https://developer.apple.com/videos/play/wwdc2022/10027/)
* [Paddle macOS install support: CPU only](https://www.paddlepaddle.org.cn/documentation/docs/en/install/pip/macos-pip_en.html)
