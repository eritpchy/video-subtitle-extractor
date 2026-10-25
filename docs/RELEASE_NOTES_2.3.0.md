# Video Subtitle Extractor 2.3.0

This is a new feature release, not a replacement for 2.2.0. Existing 2.2.0 tags,
release status and downloads remain unchanged. Publication requires completed
CI and native package verification for the exact release commit.

## Features

- Merge upstream changes through `85746f7`.
- Genuine PP-OCRv6 small and medium models, with pinned archive/file checksums,
  verified offline bundles and explicit legacy recognizers for other languages.
- VideoSubFinder CLI v6.10 on Linux, Windows and macOS, using verified portable
  downloads and preserved VSE settings.
- Persistent light, dark and system appearance, including live updates, task
  status colors, logs and timeline controls.
- Noninteractive CLI and CPU Docker runtime using the same extraction engine.

## Correctness and reliability

- Preserve recognition-box/text alignment and geometric reading order.
- Pass language/mode settings to spawned workers and preserve full-frame ROI
  coordinates when masking the upper/lower half.
- Surface OCR/decoder failures, clean up child processes and pending queues,
  and avoid hanging during error shutdown.
- Read VSF stderr through completion, validate exit status, order frame events
  correctly, and handle paths containing spaces or Unicode.
- Protect existing/concurrently created output files unless overwrite is
  explicit. Keep original subtitles intact after failed extraction.
- Work around a verified Paddle 3.3.1 CPU oneDNN/PIR incompatibility by disabling
  oneDNN by default.

## Distribution and verification

CPU archives contain source, native offline Python wheels, verified VSF runtime
and PP-OCRv6 small/medium models. They require **native Python 3.12** and are not
standalone EXE/app installers. Read [installation](USAGE.md) and
[release verification](release-and-ci.md) before running them.

The CI matrix covers Linux x64, Windows x64, macOS ARM64 and Linux amd64 Docker,
including actual OCR, video-to-SRT, GUI theme tests, and native GUI startup.
Archive verification installs dependencies offline in a fresh environment and
repeats GUI/CLI/extraction checks. A workflow definition or source-only result
does not certify a platform; check the completed run for the final commit.

GPU, DirectML/ONNX runtime acceleration, Intel macOS, Linux ARM64 and Windows
ARM64 are not certified by this CPU release matrix. Some fallback language
models require a separate first download and are rejected when missing in
strict offline mode. See [model coverage](PP-OCRv6.md).
