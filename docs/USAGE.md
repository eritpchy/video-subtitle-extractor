# Python source backends and separate one-click packages

This fork integrates upstream `YaoFANGUK/video-subtitle-extractor` through
`85746f7`, genuine PP-OCRv6 small/medium inference models, and the pinned
`eritpchy/videosubfinder-cli` v6.10.7 release. A version string in source is not
evidence that a GitHub release or all platform checks have completed.

## Run a one-click package

Extract the complete release for the matching operating system and architecture.
No customer Python, pip or virtual-environment installation is required.

- Windows: double-click `Start-VSE.bat`.
- macOS: open `Video Subtitle Extractor.app`; the complete `.app` may be moved together.
- Linux: run `Start-VSE.sh` on the supported OS baseline documented by the release.

For the current Windows launcher, choose an extraction directory without `%`.
The automated launcher contract accepts Unicode, spaces and `&!()`, but rejects
percent-containing command paths rather than risking `cmd.exe` expansion. The
native split-volume recovery tool's separate `%` path test does not certify
launching the extracted application from that path. Server 2022/2025 validation
also does not imply that every Windows client version has been tested.

Configuration, temporary extraction files and model caches use user-writable
locations, not application resources. The package includes its interpreter and
curated inference libraries; there is no first-run dependency installation.
The complete source preparation below includes all 15 pinned models, including
the v5/v3 compatibility recognizers. A package must pass its own complete-model
gates; do not infer its contents merely from a filename or an older release.
Unsigned/unnotarized builds may require the operating system's normal user
approval. This project does not disable or bypass OS security checks.

The instructions below are for developers running a source checkout, not for
customers of a self-contained release.

## Python source: no project C++ worker required

The application, GUI/CLI orchestration and Paddle/DirectML/Python CoreML OCR
adapters/processing have editable Python source. These paths do not compile or
invoke the project's C++ `native-ocr` worker. `native/ocr/` also contains optional
Native distribution sources and Python-only data tools; it is not an opaque
binary prerequisite for source users.

This is **not** a claim that all dependencies are implemented in Python.
Qt/PySide, Paddle, ONNX Runtime, OpenCV, FFmpeg and VideoSubFinder include compiled
third-party code. Supported wheels/releases supply those dependencies. Linux
also needs the Qt system libraries listed in the Dockerfile/CI. Platform package
support and final-package acceptance remain separate checks.

### Clean clone and isolated environment

Use **Python 3.12 exactly as a major/minor series**, not arbitrary 3.13+ versions.
Do not copy a virtual environment between platforms or mix backend distributions.

```sh
git clone https://github.com/eritpchy/video-subtitle-extractor.git
cd video-subtitle-extractor
python3.12 -m venv .venv
. .venv/bin/activate
```

Windows PowerShell instead:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

### Default Paddle Python backend

```sh
python -m pip install -r requirements.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.7
python -S scripts/prepare_source_models.py paddle --output build/source-paddle-models --cache build/model-downloads/paddle
python -S scripts/prepare_source_models.py verify --format paddle build/source-paddle-models
export VSE_OCR_BACKEND=paddle
export VSE_MODEL_DIR="$PWD/build/source-paddle-models"
python gui.py
```

PowerShell variable equivalents:

```powershell
$env:VSE_OCR_BACKEND = 'paddle'
$env:VSE_MODEL_DIR = "$PWD/build/source-paddle-models"
```

The source helper prepares **15 original Paddle models / 45 inference files**:
four v6 small/medium detection/recognition models, ten v5 language recognizers and
one v3 Kannada recognizer. Every official archive and each inference file is
verified against fixed SHA256 pins. This replaces the incomplete four-v6-only
source setup; `backend.tools.model_download --tiers small medium` alone does not
prepare all compatibility languages. The output must be a **new directory**,
separate from the tracked `backend/models`; use `VSE_MODEL_DIR` to select it.

Default requirements install Paddle CPU. Do not co-install Paddle CPU and GPU.
On CPU, oneDNN remains disabled by default for the reviewed Paddle 3.3.1/PIR model
combination. CUDA runtime selection and GPU hardware evidence are separate from
this CPU source setup; a CPU CI pass is not CUDA certification.

### Windows Python CUDA 11.8 or 12.6 source backend

Use **Windows x64, CPython 3.12 and a new virtual environment**. Choose exactly
one CUDA variant. Do not first install `requirements.txt` (which contains CPU
Paddle), or install both CUDA variants into the same environment.

The source profiles include `requirements-source-common.txt`, synchronized with
`requirements.txt` except for removal of CPU Paddle. They retain **full PySide6**
and normal dependency metadata. Do not use the distribution packager's Essentials
substitution, `--no-deps`, pruning or waived `pip check` for a normal source install.
Paddle GPU is 3.3.1 from the official CUDA-specific Windows CPython 3.12 wheel;
CUDA 12.6 additionally fixes the already-reviewed NVJitLink 12.6.85 dependency.
The exact vendor-wheel URLs and complete-wheel SHA256 values are recorded in
[`source-cuda-wheel-pins.json`](audits/source-cuda-wheel-pins.json). Paddle hashes
were observed from complete wheel bytes in the accepted build, not published
vendor signatures. These key wheel pins do not turn the other ordinary source
dependency ranges into a fully hash-locked transitive environment.

A compatible NVIDIA GPU and **system NVIDIA driver** are prerequisites for CUDA
execution. The pip environment supplies its CUDA/cuDNN libraries, not the kernel
GPU driver. These commands do not install a compiler or this project's C++ worker.
No true CUDA hardware inference pass is claimed here. The existing Server 2022/2025
package CPU tests are not certification of this new source-install recipe.

PowerShell, after cloning the repository:

```powershell
# Choose ONE matching pair before creating the environment:
$CudaRequirements = 'requirements-source-cuda118.txt'
$CudaVersion = '11.8'
# For CUDA 12.6 instead:
# $CudaRequirements = 'requirements-source-cuda126.txt'
# $CudaVersion = '12.6'

py -3.12 -m venv .venv-cuda
.venv-cuda\Scripts\Activate.ps1
python -c "import platform,struct,sys; assert sys.implementation.name == 'cpython' and sys.platform == 'win32' and sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64','x86_64')"
python -m pip install -r $CudaRequirements
python -m pip check
python -c "import importlib.metadata as m; assert m.version('paddlepaddle-gpu') == '3.3.1'; assert not any(d.metadata['Name'].lower() == 'paddlepaddle' for d in m.distributions())"
python scripts/install_videosubfinder.py --version 6.10.7
python -S scripts/prepare_source_models.py paddle --output build/source-cuda-models --cache build/model-downloads/paddle
python -S scripts/prepare_source_models.py verify --format paddle build/source-cuda-models
$env:VSE_OCR_BACKEND = 'paddle'
$env:VSE_MODEL_DIR = "$PWD/build/source-cuda-models"

# This checks compilation/device discovery, NOT an OCR/GPU inference pass:
python -c "import paddle; assert str(paddle.version.cuda()) == '$CudaVersion'; assert paddle.is_compiled_with_cuda(); assert paddle.device.cuda.device_count() > 0, 'No usable CUDA device: check hardware/driver before requesting GPU'"
python gui.py
# GPU requested; do not add --cpu to this command:
python -m backend.cli "input.mp4" --language en --mode auto
# Explicit CPU through the SAME Paddle GPU environment and model data:
python -m backend.cli "input.mp4" --language en --mode accurate --cpu
```

If the device check fails, do not describe a later CPU result as CUDA execution.
Paddle's availability policy can otherwise select CPU. Successful installation,
`pip check` and device discovery are distinct from real workload correctness.
The fixed model-data output must be new; for an existing verified output, rerun
`verify` and reuse its `VSE_MODEL_DIR` rather than overwriting it.

The documented wheel/index selection follows `scripts/release_profiles.py` and
the reviewed wheel inventory, not the stale CUDA 10.2 installation prose embedded
in upstream wheel metadata. Current source CUDA support is **11.8/12.6 only**;
CUDA 10.2 is retained solely as the immutable historical 2.2.0 release download.

### Windows DirectML Python backend

Use a separate Python 3.12 virtual environment. The current source requirements
are `requirements-directml-inference.txt`, not the historical converter recipe
`requirements_directml.txt`. The supported DirectML runtime remains 1.20.1.

```powershell
python -m pip install -r requirements-directml-inference.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.7
python -S scripts/prepare_source_models.py onnx --output build/source-onnx-models --cache build/model-downloads/onnx
python -S scripts/prepare_source_models.py verify --format onnx build/source-onnx-models
$env:VSE_OCR_BACKEND = 'directml'
$env:VSE_DIRECTML_MODEL_ROOT = "$PWD/build/source-onnx-models"
python gui.py
# Explicit CPU through the same Python backend/model data:
python -m backend.cli "input.mp4" --language en --mode accurate --cpu
```

### macOS arm64 Python CoreML backend

Use another Python 3.12 environment and `requirements-coreml-inference.txt`.
Do not combine it with the DirectML 1.20.1 environment. This is the Python ORT
adapter, **not** `VSE_OCR_BACKEND=native` and not a C++ worker invocation.

```sh
python -m pip install -r requirements-coreml-inference.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.7
python -S scripts/prepare_source_models.py onnx --output build/source-onnx-models --cache build/model-downloads/onnx
python -S scripts/prepare_source_models.py verify --format onnx build/source-onnx-models
export VSE_OCR_BACKEND=python-coreml
export VSE_PYTHON_COREML_MODEL_ROOT="$PWD/build/source-onnx-models"
python gui.py
# Explicit CPU, without switching to a native worker or substituting models:
python -m backend.cli "input.mp4" --language en --mode accurate --cpu
```

Provider initialization/execution failure must remain an error, not silent CPU
fallback. Backend registration and matching model hashes do not establish OCR
accuracy. In particular, earlier Native worker/CoreML results do not certify
this Python implementation: require the matching source backend's actual Mac
checks. CoreML EP execution alone does not prove physical GPU dispatch or the
provider's internal precision.

### Complete ONNX data, public/offline source and integrity

Both ONNX source backends use the same **15 FP32 graphs, 13 JSON configs and two
model metadata files**. The stdlib helper reads the public fixed release asset
[vse-2.3.0-linux-x64-native-cpu.zip](https://github.com/eritpchy/video-subtitle-extractor/releases/download/2.3.0/vse-2.3.0-linux-x64-native-cpu.zip):

- Whole ZIP: **517,238,092 bytes**; SHA256
  `60e6f8098c058777273b5a6820d804731b5c93c45c78b14f5d8d1d1165361a11`.
- Only the exact 30 model-data files under `native-ocr/` are extracted. The Linux
  worker, Python runtime, libraries and other executable contents are **not**
  extracted, installed, imported or executed on any platform.
- The entire ZIP and all selected files/manifests are independently checked.
  Data provenance is not new model conversion or inference evidence. No model
  pins, precision, language routes or graph bytes are changed.
- This uses a public release, not an expiring Actions artifact: no GitHub login,
  token, native compiler or client model converter is required. The first
  download is approximately 517 MB; later preparations can reuse its verified
  cache. A download/access error fails rather than switching to another source.

To reuse an existing original ZIP without networking:

```sh
python -S scripts/prepare_source_models.py onnx --output build/another-new-model-root --archive /path/to/vse-2.3.0-linux-x64-native-cpu.zip --offline
```

Paddle preparation likewise supports `--offline` with a complete verified
archive cache. Existing outputs, corrupt caches, links, missing models and hash
mismatches are rejected rather than overwritten or repaired from unapproved
sources. Runtime source paths only consume the prepared local data.

### Reproduce ONNX conversion from the original models (Linux build-side route)

The public ZIP is a convenient data source, not the only source of the models.
The repository retains exact original archive/file/ONNX/config hashes and the
Python conversion recipe. In a separate **Linux x64 / Python 3.12** build venv:

```sh
python -m pip install -r native/ocr/requirements-model-build.txt
python native/ocr/prepare_models.py --output build/rebuilt-onnx --cache build/original-model-archives
python -S native/ocr/verify_model_cache.py build/rebuilt-onnx
```

These are Python data tools; do not run `native/ocr/build.py`, CMake or Ninja to
prepare source models. The converter enforces the reviewed Paddle2ONNX version,
FP32 bytes, opset 17, no optimizer/quantization and all 15 outputs. Its output also
includes a build-only `unavailable-models.json` marker; the Python backends read
the same 30 model/config/metadata data files. Set the chosen backend's model-root
variable to `build/rebuilt-onnx` after verification. This documented deterministic
conversion route is Linux-based; it does **not** claim Mac Paddle2ONNX conversion
has been validated. Build-time converter packages are not runtime dependencies.

Fast mode uses v6 small where supported; accurate uses medium. **Auto uses medium
only when Paddle CUDA is enabled and available. CPU, DirectML and Python/Native
CoreML auto remain small**, even when those non-CUDA providers accelerate execution.
Languages outside v6 coverage retain their explicit v5/v3 recognizers. GPU use
alone does not imply higher accuracy for the same mode/model; model selection and
actual inference correctness are separate. See [model coverage](PP-OCRv6.md).

The VideoSubFinder installer independently checks the official release sums and
archive against its committed lock and preserves required platform libraries.
See [VideoSubFinder integration](VIDEO_SUBFINDER.md).

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

CI distinguishes Python source backends from Native one-click packages, and
CPU parity from actual accelerator execution. Consult the exact commit and
backend's completed runs; a Native worker pass cannot certify Python CoreML.
Windows verification uses **Server 2022 and Server 2025 x64** only. Windows 10/11
clients, Windows ARM/emulation and physical GPU execution are not established by
those server CPU checks. No client-machine or GPU access is requested or awaited.
See the [Windows coverage boundaries and
evidence guide](windows-verification.md).
