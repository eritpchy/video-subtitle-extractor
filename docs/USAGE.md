# One-click CPU packages and source development

This fork integrates upstream `YaoFANGUK/video-subtitle-extractor` through
`85746f7`, genuine PP-OCRv6 small/medium inference models, and the pinned
`eritpchy/videosubfinder-cli` v6.10.6 release. A version string in source is not
evidence that a GitHub release or all platform checks have completed.

## Run a one-click package

Extract the complete release for the matching operating system and architecture.
No customer Python, pip or virtual-environment installation is required.

- Windows: double-click `Start-VSE.bat`.
- macOS: open `Video Subtitle Extractor.app`; the complete `.app` may be moved together.
- Linux: run `Start-VSE.sh` on the supported OS baseline documented by the release.

Configuration, temporary extraction files and model caches use user-writable
locations, not application resources. The package includes its interpreter and
curated inference libraries; there is no first-run dependency installation.
Some optional legacy language models have never been bundled; consult
[model coverage](PP-OCRv6.md) before relying on those languages offline.
Unsigned/unnotarized builds may require the operating system's normal user
approval. This project does not disable or bypass OS security checks.

The instructions below are for developers running a source checkout, not for
customers of a self-contained release.

## Install from source

Use native Python 3.12 and an isolated virtual environment. Linux needs the Qt
runtime libraries listed in the Dockerfile/CI workflow. Do not copy a virtual
environment between Linux, Windows and macOS.

```sh
python3.12 -m venv .venv
# Linux/macOS:
. .venv/bin/activate
# Windows PowerShell instead: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip check
python scripts/install_videosubfinder.py
python -m backend.tools.model_download --tiers small medium
python gui.py
```

Model archives and every inference file are checked against the committed
SHA-256 manifest. The VideoSubFinder installer checks the matching official
release's SHA256SUMS against the committed release lock, checks the archive,
preserves the native package's libraries, and migrates old
VSE settings. See [VideoSubFinder integration](VIDEO_SUBFINDER.md).

The default requirements install the CPU Paddle runtime. GPU/DirectML builds
are not certified by the CPU CI matrix. An installed ONNX provider is not proof
that this Paddle OCR path uses that accelerator. On CPU, oneDNN is disabled by
default because the tested Paddle 3.3.1/PIR combination failed with v6 models.

PP-OCRv6 small is used for fast mode and CPU auto mode; medium is used for
accurate mode and GPU auto mode. Languages outside v6 coverage retain explicit
compatible recognizers rather than being silently relabeled as v6. On a clean
source checkout, download the model tier before use; offline packages bundle
both tiers. See the model manifest and model documentation for exact sources.

## Headless CLI

```sh
python -m backend.cli "input video.mp4" --output "subtitles.srt" \
  --language en --mode fast --cpu --threads 2 --area 0.65,1,0,1
```

- `--area` is **ymin,ymax,xmin,xmax**, normalized to `[0,1]` from the top-left.
  This differs from VideoSubFinder's bottom-relative `-te`/`-be` options; the
  adapter translates them. The default is the bottom 35% of the frame.
- Paths containing spaces and Unicode are supported. An existing output is
  preserved unless `--overwrite` is explicit, and failed extraction does not
  replace it.
- CLI settings are passed to spawned OCR workers without changing saved GUI
  preferences. OCR/decoder failures return a nonzero exit status.
- Use `--keep-work NEW_DIRECTORY` to retain intermediate data. An existing
  directory is rejected so cache cleanup cannot erase unrelated files.
- `--no-word-segmentation` preserves OCR spacing. `--help` and `--version` do
  not load Qt, Paddle, or download models.

## Light, dark and system appearance

Choose light, dark or system appearance in Advanced Settings. Changes apply
without restarting and are saved across launches. System changes do not
override a manual light/dark choice. The video canvas intentionally remains
black in both themes.

## Docker

```sh
docker build -t video-subtitle-extractor .
docker run --rm -v "$PWD/videos:/data" video-subtitle-extractor \
  /data/input.mp4 --output /data/input.srt --language en --cpu
```

The image is CPU/headless and runs as a non-root user. Ensure the mounted output
directory is writable by that user. Docker image building and container
execution are separate checks from a Linux source test, and must both pass
before claiming Docker verification.

## Reproducible checks

```sh
python -m pip install pytest
QT_QPA_PLATFORM=offscreen python -m pytest tests -q
QT_QPA_PLATFORM=offscreen python gui.py --smoke-test
python scripts/e2e_smoke.py --output artifacts/e2e
```

Use the equivalent environment-variable syntax on Windows. Set
`VSE_RUN_OCR_INTEGRATION=1` to enable actual OCR model inference tests. The E2E
script generates a known video, runs the actual CLI/VSF/PP-OCRv6 pipeline, and
checks recognized words, timestamp bounds, and non-overlap. It saves the video,
SRT, extraction log and JSON result. Mock/unit tests alone do not establish OCR
accuracy, native GUI support, or a successful release.

CI configures Linux x64, Windows **Server 2022 and Server 2025 x64**, and macOS
ARM64 CPU checks plus Docker; consult the exact commit's completed Actions run
for actual results. Paddle and native CPU profiles are separate. Windows 10/11
x64 clients, ARM Linux, Intel macOS, GPU acceleration and Windows ARM/emulation
are outside the selected verified coverage. Windows verification is deliberately
Server CI only; client/ARM/GPU machine access is not requested or awaited.
See the [Windows coverage boundaries and
evidence guide](windows-verification.md).
