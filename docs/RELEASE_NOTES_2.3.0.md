# Video Subtitle Extractor 2.3.0

This is a new feature release, not a replacement for 2.2.0. Existing 2.2.0 tags,
release status and downloads remain unchanged. Publication requires completed
CI and native package verification for the exact release commit.

## Features

- Merge upstream changes through `85746f7`.
- Genuine PP-OCRv6 small and medium models, with pinned archive/file checksums,
  verified offline bundles and explicit legacy recognizers for other languages.
- VideoSubFinder CLI v6.10.6 on Linux, Windows and macOS, using verified portable
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
- Keep portable configuration independent of the launch directory (upstream
  [#450](https://github.com/YaoFANGUK/video-subtitle-extractor/issues/450)).
- Preserve visual subtitle rows through OCR grouping and word segmentation,
  without rewriting the raw recognition cache (upstream
  [#467](https://github.com/YaoFANGUK/video-subtitle-extractor/issues/467)).
- Decode otherwise unsupported OpenCV frames with bundled FFmpeg; when needed,
  prepare a lossless temporary stream for VSF rather than report empty success
  (upstream [#459](https://github.com/YaoFANGUK/video-subtitle-extractor/issues/459)).
  This compatibility conversion is bounded to 30 minutes and the smaller of
  20 GiB or 80% of available temporary disk space. Inputs exceeding these limits
  require manual pre-conversion; corrupt/unsupported media fail explicitly.
- Launch timeline synchronization from any working directory, preserve source
  subtitles, refuse existing output overwrites, and ignore stale completion
  callbacks after stopping or restarting a job. New outputs use `.synced.srt`
  or `.synced.ass` rather than overwriting the source filename.
- Preserve the original rounded logo and transparent corners in all seven
  ICO sizes, window icons and application icons.

## Distribution and verification

CPU archives contain source, a portable Python interpreter and preinstalled inference libraries, verified VSF runtime
and PP-OCRv6 small/medium models. Customers do not install Python, pip or a
virtual environment; launchers use the included interpreter. Read [usage](USAGE.md) and
[release verification](release-and-ci.md) before running them.

The CI matrix covers Linux x64, Windows x64, macOS ARM64 and Linux amd64 Docker,
including actual OCR, video-to-SRT, GUI theme tests, and native GUI startup.
Archive verification runs the included interpreter without system Python and
repeats GUI/CLI/extraction checks. A workflow definition or source-only result
does not certify a platform; check the completed run for the final commit.
Issue-specific real-media gates additionally cover multiline subtitles, AV1
extraction and an exact two-second timeline shift, including offline Docker
and freshly extracted self-contained archive execution.

GPU, DirectML/ONNX runtime acceleration, Intel macOS, Linux ARM64 and Windows
ARM64 are not certified by this CPU release matrix. Some fallback language
models require a separate first download and are rejected when missing in
strict offline mode. See [model coverage](PP-OCRv6.md).
