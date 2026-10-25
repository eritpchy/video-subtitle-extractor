# macOS arm64 FP32 confidence investigation

The frozen `1e-5` confidence tolerance, exact text/boxes/order, finite-score checks,
models, and production session settings are unchanged. A completed diagnostic
is **not** a passing parity gate or permission to publish a customer package.

## Established evidence

Run 40179636440, commit `f5f3f6099822246c511b10299dbbe4c8e8c5b5bc`, compiled on
AppleClang 17.0.0.17000013, CMake 4.1.2, macOS 15.7.9 arm64, OpenCV 4.10.0,
official ORT 1.31.0. Four of 110 cases exceeded confidence tolerance:

| Case | Model / detector tier / fixture | Absolute confidence difference |
|---|---|---:|
| 31 | Cyrillic / medium / script | 0.00007236003875732422 |
| 33 | Cyrillic / medium / wide | 0.00011140108108520508 |
| 66 | Thai / small / wide | 0.00002384185791015625 |
| 72 | Tamil / small / wide | 0.0001830458641052246 |

All four retained exact boxes/text/order and finite scores; all 19 protocol edges
passed. This is not yet enough to classify the differences as acceptable numeric
drift. The original artifact lacked the actual executable, complete stage
inventory, verbose compile commands and exact macOS SDK version; the diagnostic
job now preserves these metadata gaps where available.

The worker explicitly uses `-ffp-contract=off -fno-fast-math`, but these flags do
not rebuild or control arithmetic inside the precompiled ORT library. No CoreML
provider is appended. ORT 1.31 documents ARM64 BF16 GEMM fastmath as **off by
default**; it is not enabled by the worker. See the official
[versioned configuration definitions](https://github.com/microsoft/onnxruntime/blob/v1.31.0/include/onnxruntime/core/session/onnxruntime_session_options_config_keys.h).
Thus neither hidden BF16 nor CoreML is an established cause.

Architecture-specific FP32 kernels and graph layout remain hypotheses, not
findings. ORT's [versioned platform dispatch](https://github.com/microsoft/onnxruntime/blob/v1.31.0/onnxruntime/core/mlas/lib/platform.cpp)
selects ARM/NEON implementations, and its
[optimization documentation](https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html)
distinguishes basic, extended and layout passes. The OpenCV build also reports
Carotene HAL; its `NEON FP16` capability label is not evidence that these FP32
inferences ran in FP16. Exact preprocessing tensors must be compared as well.

## Minimal next CI experiment

The existing smoke step remains hard-failing. Only after a macOS smoke failure:

1. Opt into the separate `native-ocr-precision` CMake target, reusing already
   compiled OpenCV and the same SDK. It is excluded from the default build.
2. Run the four failures plus eight same-model controls through production,
   two fresh baseline diagnostic runs, explicit BF16-off, EXTENDED, BASIC,
   DISABLED and OpenCV-unoptimized variants. Capture responses and exact
   little-endian FP32 detector/recognizer input/output bytes with hashes/shapes.
   The diagnostic target also saves integer detector quads, the crop's second
   `minAreaRect` corners, the exact `warpPerspective` matrix and the resulting
   BGR uint8 crop before recognition resize (raw bytes, shape and verified SHA).
   This distinguishes geometry changes from warp changes and later resizing;
   it does not assume which operation caused the current discrepancy.
3. In a separate venv, install the hash-pinned official macOS arm64 Paddle
   3.3.1 wheel and pinned dependencies. Replay those same recognition tensors
   through the original hash-verified Paddle sources, CPU FP32, four threads,
   oneDNN off, `ir_optim` flag false (Paddle still runs mandatory PIR passes;
   preserve its log). Save raw output, token/confidence differences
   and provenance. This isolates runtime differences from preprocessing;
   it is not independent whole-image end-to-end validation.

Neither the diagnostic executable, Paddle environment nor tensor dumps enter
`build/native-ocr` or the customer dependency closure. Evidence is saved under
`test-results/native-precision`; source downloads are build-cache-only.

## How to interpret the result

- Baseline must reproduce the production response; repeat hashes test stability.
- Different recognition-input hashes identify a preprocessing/crop path to
  investigate before attributing score drift to ORT arithmetic.
- Identical inputs but different runtime outputs localize the issue to inference.
- Improvement in EXTENDED but not ALL isolates the layout-optimization family;
  it does not by itself prove a single kernel defect or authorize a production
  optimization change.
- Same-input Paddle agreement is a valuable independent reference, not an
  automatic replacement golden. Any eventual architecture-specific policy needs
  broader held-out evidence and confidence-threshold stability analysis. Do not
  choose a tolerance just above these four observed errors.

Diagnostics and full packaged GUI/video/SRT acceptance are separate results.
Linux helper/compile checks cannot establish macOS acceptance.

## Round 2: input-level localization, not tolerance relaxation

The `1d965e5` macOS run completed all 96 selected requests (production plus seven
diagnostic variants, 12 cases each). Default traces reproduced production;
BF16-off, EXTENDED/BASIC/DISABLED and OpenCV-unoptimized did not remove the four
confidence failures. The separate Paddle environment installed successfully,
but the official source archive server returned HTTP 403 on Mac **before any
Paddle reference inference**. Do not report that stage as a reference pass.

Verified Mac trace tensors were compared with the preserved Linux captures:
all 12 detector input tensors were byte-identical, and all 12 detector threshold
masks (`prediction > .3`) were identical. Recognition inputs differed for the
four failed cases and two passing Tamil-medium controls. Replaying both input
sets with the same Linux ORT 1.31 CPU session separates input changes from
recognizer runtime differences:

| Case | Changed FP32 input elements | Confidence delta: input change only, same Linux ORT | Confidence delta: same Mac input, Mac-native vs Linux ORT |
|---|---:|---:|---:|
| 31 | 24 | 0.0000720024 | 0.0000004768 |
| 33 | 27 | 0.0001117587 | 0.0000003576 |
| 66 | 21 | 0.0000237226 | 0.0000001788 |
| 72 | 165 | 0.0001828671 | 0.0000001788 |

The largest input differences correspond to 2/3/4/5 uint8 gray levels,
respectively. Across all 12, replaying the Mac inputs through independent Linux
Paddle 3.3.1 kept the maximum confidence difference below `2.03e-6`. Linux
Python ORT reproduced the original Linux native tensors byte-for-byte.

This localizes the dominant four-case discrepancy to the recognition input
construction path, not recognizer ARM arithmetic. It does **not** yet distinguish
crop geometry, perspective warping, and resizing, and it is **cross-architecture
causal evidence, not same-Mac Paddle validation**. Goldens and `1e-5` remain fixed.

To remove the Mac download blocker, a separate Linux diagnostic job exports exactly
three original Paddle recognizer sources as a separate build-only artifact,
using the existing official archive and per-file pins. Its independently keyed
archive cache works even when converted models are a cache hit. Mac verifies
the files against committed pins again and performs reference inference offline;
there is no mirror, weaker hash policy, or customer-package dependency change.
Windows/Linux native jobs depend only on the verified 15-model conversion job,
not this diagnostic source job. After a Mac smoke failure, only the Mac runner
waits for the same-run source job's result. Failure or unavailable reference
evidence is recorded explicitly and never treated as a parity pass; the original
smoke remains a hard failure.

## Round 3: crop geometry contraction, not inference tolerance

Commit `0a858b5` completed the real Mac geometry capture **and** all 84 independent
same-Mac Paddle tensor replays using verified offline original sources. The four
baseline native/Paddle confidence differences were approximately `1.43e-6`,
`2.03e-6`, `9.54e-7`, and `1.79e-7`, with identical CTC tokens. The original global
goldens still failed the same four cases: reference agreement is not a gate bypass.

All 12 integer detector quads, crop dimensions and rotation decisions matched
Linux. Floating-point corners from the crop's second `minAreaRect`/`points` path
already differed before perspective warping. Failed cases 31/33/66/72 had only
6/12/6/63 differing pre-resize BGR bytes, respectively (maximum value delta 6).

A controlled Linux replay of the **captured Mac corners** through the existing
OpenCV `getPerspectiveTransform` and `warpPerspective` reproduced the Mac uint8
crops byte-for-byte for **12/12** cases. Captured Linux corners reproduced all
Linux crops. Resizing and normalizing the captured Mac crops with the existing
Linux implementation also reproduced **12/12 Mac recognition inputs exactly**.
Thus corner differences suffice to explain the downstream differences; there is
no evidence requiring a different warp, resize, or Carotene path.

The pinned OpenCV [corner implementation](https://github.com/opencv/opencv/blob/4.10.0/modules/core/src/types.cpp)
contains FP32 multiply/add expressions. Re-evaluating these expressions with
nested FP32 FMA on the Linux rectangle parameters exactly reproduced 11/12 Mac
corner sets. Recompiling the original
[rotating-calipers implementation](https://github.com/opencv/opencv/blob/4.10.0/modules/imgproc/src/rotcalipers.cpp)
with FMA enabled also changed case 72's rectangle parameters; those parameters
plus the same FMA corner evaluation reproduced **12/12 Mac corner sets exactly**.
Conversely, compiling those original source objects with FMA hardware available
but `-fno-fast-math -ffp-contract=off` retained 12/12 original Linux corner sets.
The Linux native worker relinked with those strict objects passed all 110 frozen
parity cases and 19 edges (maximum confidence difference `1.79e-7`). This is a
causal Linux reproduction, not execution of a corrected Mac build.

[Clang documents](https://clang.llvm.org/docs/UsersManual.html#controlling-floating-point-behavior)
that default contraction permits fused operations within an expression, while
`-ffp-contract=off` prohibits them. The worker's flags do not control separately
compiled OpenCV source. The narrowly scoped candidate therefore applies
`-fno-fast-math -ffp-contract=off` to the **Darwin OpenCV build**, retaining SIMD,
HAL, interpolation, model weights, texts/boxes and the `1e-5` gate. Compile
commands and CMake cache are preserved even if the next Mac smoke succeeds.
Actual corrected-Mac parity remains pending; only the unchanged full smoke can
establish that the four-case gap is resolved.
