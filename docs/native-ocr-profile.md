# Experimental compact native OCR profile

Default inference remains Paddle. The optional `native` backend uses the pinned
FP32 models through a separately built C++ CPU worker. It does not import Paddle,
PaddleX, PaddleOCR or Python ONNX Runtime. This is not yet a released compact
one-click package; native Windows/macOS builds and dependency closure remain gates.

Explicit opt-in (set environment variables in your launch environment):

```
VSE_OCR_BACKEND=native
VSE_NATIVE_OCR_ROOT=/absolute/path/to/staged/native-worker
VSE_NATIVE_OCR_PROVIDER=cpu
```

The root must contain `native-ocr` (`native-ocr.exe` on Windows), its native loader
libraries, `models/`, `config/`, and `model-manifest.json`. Paths may contain spaces
and Unicode. No shell or customer-installed Python is used to launch the worker;
the GUI's bundled Python hosts the adapter. No runtime/model downloads occur.
`VSE_NATIVE_OCR_TIMEOUT` bounds the whole request including model loading (default
120 seconds, maximum 120). `VSE_OCR_CPU_THREADS` selects 1..128 native CPU threads.
Recognition batch size follows the existing application setting.

Only validated, installed language/model pairs may run. Missing assets fail with
an explicit error, never silently switch language/model/tier. `auto` on this CPU
backend uses small, `fast` small, `accurate` medium. CUDA/DirectML/CoreML provider
requests are explicitly rejected: this CPU profile is not a replacement for CUDA.

Transport uses bounded binary-framed UTF-8 JSON and lossless temporary PNGs in the
user temporary directory, not the read-only install directory. Requests are serial
and keep one native process alive. Errors are propagated without silent retry;
a subsequent call may restart a failed worker. Cancellation/timeout/close kill
and reap the process, and extraction closes recognizers in `finally` paths.
ORT telemetry is disabled in the environment before process launch and in the
native API. A clean no-network container is still a release gate; this is not a
claim that syscall-level network tracing has been performed.

Run focused contracts with `pytest tests/test_native_ocr.py`. Real image/detection
and ordinary/multiline/AV1 video-to-SRT tests:

```
VSE_RUN_NATIVE_INTEGRATION=1 VSE_NATIVE_OCR_ROOT=/staged/native-worker \
  pytest tests/test_native_ocr_integration.py
```

These run a subprocess import blocker: loading Paddle/PaddleX/PaddleOCR or Python
ORT fails the test, including spawned OCR processes. Do not confuse native-worker
size with final GUI + VSF + FFmpeg + model + runtime distribution size.

The tested optional registry includes the four V6 small/medium models plus eleven
fallback recognizers: Arabic, Cyrillic, Devanagari, Greek, East Slavic, Korean,
Latin/Pali, Thai, Tamil, Telugu, and Kannada. Selection still uses
`PaddleModelConfig`, not a second language map. The formerly missing Tamil, Telugu,
and Kannada assets have now been verified independently and may be included in
the complete native stage. Smaller stages fail explicitly when an asset is absent.
In this project both `ka` and `kn` are existing Kannada aliases (including the UI
labels); their preservation does **not** add or claim Georgian recognition.

Arabic workers return logical-order CTC text with `text_order=logical`; the
adapter applies the existing `bidi.algorithm.get_display` exactly once. A
`display` response is not reordered again. Missing order metadata is an error.
No other model receives bidi reordering or Unicode normalization. Full legacy
adapter parity can be exercised with `VSE_NATIVE_LEGACY_EVIDENCE` pointing to the
pinned conversion evidence/fixtures directory (48 full-pipeline comparisons),
and `VSE_NATIVE_MISSING_EVIDENCE` for the newly recovered three models (18 more).

Standalone detection preserves its own PaddleX profile (960/max and pinned YAML
thresholds), not the OCR pipeline detector profile (64/min and pipeline thresholds).
Set `VSE_NATIVE_DETECTION_EVIDENCE` to the native worker evidence directory to run
the 22-case exact standalone polygon parity test. Model weights **and** JSON
configuration/dictionary hashes are checked before launching; a missing or altered
manifest entry fails closed. Final package verification must also authenticate the
manifest itself. Native parent-death protection uses the automatically supplied
`VSE_OCR_PARENT_PID`; this is not a customer setup step.
