# Genuine PP-OCRv6 support

The 2.3.0 runtime uses **PaddleOCR 3.7.0**, **PaddleX 3.7.2**, and
**PaddlePaddle 3.3.1**, not renamed v5 weights. The models are the official
PPLCNetV4 PP-OCRv6 inference bundles released with
[PaddleOCR 3.7.0](https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0).
See the [official v6 model documentation](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html).

## Model selection

- Fast: `PP-OCRv6_small_det` and `PP-OCRv6_small_rec`.
- Accurate: `PP-OCRv6_medium_det` and `PP-OCRv6_medium_rec`.
- Auto: medium on an enabled CUDA device; small on CPU.
- Small/medium support Chinese (simplified/traditional), English, Japanese,
  and the official Latin-script language group. Tiny is deliberately not used:
  it does not recognize Japanese.
- Korean, Arabic, Cyrillic, Devanagari, Pali, Thai, Greek, Tamil and Telugu use
  their explicit official PP-OCRv5 recognition models, with v6 detection.
  Russian/Belarusian/Ukrainian use the more specific `eslav` model.
- Kannada (`kn`, legacy alias `ka`) uses `ka_PP-OCRv3_mobile_rec`.
  Unsupported codes fail explicitly rather than silently selecting Chinese.

The log prints the actual detection and recognition model names. Language
fallbacks are **not** reported as v6 recognizers.

## Reproducible weights and offline packages

```sh
python -m backend.tools.model_download --tiers small medium
python -m backend.tools.model_download --tiers small medium --verify-only
```

`backend/models/ppocrv6-manifest.json` records the official Baidu BOS URLs and
SHA256 for each archive **and each inference file**. The downloader validates
all hashes, extracts only the three expected regular files, and publishes the
verified directory atomically under a per-model lock. Incomplete/modified
existing directories fail without destroying their contents.

Source checkouts need not commit 163 MiB of model binaries. Release packaging
must run the prefetch command and include `backend/models/V6`. Existing bundled
v5 language models remain available. Additional unbundled fallback language
models are obtained by PaddleX using their explicit official names.

At runtime bundled models are preferred. Missing v6 models are downloaded from
the same pinned manifest into `~/.cache/video-subtitle-extractor/models/V6`,
not into a possibly read-only application directory. `VSE_MODEL_DIR` overrides
the bundled models root and `VSE_MODEL_CACHE` overrides the writable cache root.
`VSE_OFFLINE=1` prohibits missing v6 downloads and gives a prefetch instruction.
For fully offline operation, also pre-bundle any selected v5/v3 fallback models.

## Device and CPU compatibility

Paddle CPU and NVIDIA CUDA are supported. The UI reports only the device the
inference code actually uses. Merely installing a DirectML/CoreML/ONNX provider
does not make this Paddle path GPU-accelerated. ONNX acceleration remains a
separate, unverified deployment option, not a promised fallback.

The tested Paddle 3.3.1 CPU oneDNN path raises a PIR `DoubleAttribute` error on
v6. Therefore the adapter defaults to `enable_mkldnn=False`; `VSE_ENABLE_MKLDNN=1`
is an explicit opt-in for runtimes where that issue has been fixed. CPU thread
count defaults to min(4, available cores), configurable with
`VSE_OCR_CPU_THREADS` (positive integer). Recognition batches honor the GUI/CLI
setting in spawned workers.

## Verification

```sh
python -m pytest -q tests/test_paddle_models.py tests/test_model_download.py tests/test_ocr.py tests/test_subtitle_ocr_worker.py
VSE_RUN_OCR_INTEGRATION=1 python -m pytest -q tests/test_ocr_integration.py
python scripts/e2e_smoke.py
```

The opt-in real-model suite asserts recognized text for English, Chinese and
Japanese with v6 small, mixed English/Chinese with v6 medium, and Korean with
its v5 fallback. It also checks blank images and actual v6 detection. The
video smoke exercises decoding, subtitle timing and SRT content, rather than
only constructing OCR objects.

Unit tests cover model identity, language/mode selection, checksum failure,
atomic downloads, filtered `rec_polys` alignment, geometric reading order,
empty output, device consistency, worker settings and failure/cancellation.
The adapter uses recognition polygons (not unfiltered detection polygons),
keeps text/score/box associations intact, and preserves pixel coordinates.
Worker failures produce a nonzero process exit and never publish a partial raw
subtitle file as successful output.
