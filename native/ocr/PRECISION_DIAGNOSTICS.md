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

macOS diagnostic and full packaged GUI/video/SRT results remain pending until
the real macOS CI run executes. Linux helper/compile checks cannot establish them.
