> This fork adds genuine PP-OCRv6 small/medium, VideoSubFinder CLI v6.10.5, light/dark/system themes and a noninteractive CLI. See the [current runtime guide](docs/USAGE.md) for installation, Docker and tests. Release/platform verification is tied to the exact commit's GitHub Actions results.

[简体中文](README.md) | English

<p align="center"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/icon_1024.png" alt="VSE Logo" width="128"/></p>

## Introduction

![License](https://img.shields.io/badge/License-Apache%202-red.svg)
![python version](https://img.shields.io/badge/Python-3.12+-blue.svg)
![support os](https://img.shields.io/badge/OS-Windows/macOS/Linux-green.svg)

Video-subtitle-extractor (VSE) is a software that extracts hard-coded subtitles from videos into external subtitle files (SRT format).

Key features:

- Extract key frames from videos
- Detect text positions in video frames
- Recognize text content in video frames
- Filter out non-subtitle text regions
- [Remove watermarks, channel logos, and original hard-coded subtitles — use with: video-subtitle-remover (VSR)](https://github.com/YaoFANGUK/video-subtitle-remover/tree/main)
- Remove duplicate subtitle lines and generate SRT subtitle files / TXT text files
- Support **batch extraction** of video subtitles
- Multi-language: supports subtitle extraction in **87 languages**, including **Simplified Chinese (Chinese-English bilingual)**, **Traditional Chinese**, **English**, **Japanese**, **Korean**, **Vietnamese**, **Arabic**, **French**, **German**, **Russian**, **Spanish**, **Portuguese**, **Italian**, and more
- Multiple modes:
  - **Fast**: (Recommended) Uses a lightweight model for quick subtitle extraction. May miss a small number of subtitles and have minor typos.
  - **Auto**: (Recommended) Automatically selects the model — uses the lightweight model on CPU and the precise model on GPU. Slower extraction speed, may miss a small number of subtitles, but with almost no typos.
  - **Precise**: (Not recommended) Uses the precise model with frame-by-frame detection on GPU. No missed subtitles and almost no typos, but **very slow**.

> Please use Fast/Auto mode first. Only switch to Precise mode if the first two modes miss too many subtitle lines.

<p style="text-align:center;"><img src="design/demo.png" alt="demo.png"/></p>

**Highlights**:

- Uses local OCR recognition — no need to configure any API or connect to online OCR services such as Baidu or Alibaba
- Supports GPU acceleration for higher accuracy and faster extraction speed

**Usage Guide**:

- For questions, please join the discussion group (QQ Group): 210150985 (full), 806152575 (full), 816881808 (full), 295894827

- Click [Open] to select video files, adjust the subtitle area, then click [Run]
  - Single file extraction: Select **one** video file when opening
  - **Batch extraction**: Select **multiple** video files when opening. Ensure all videos have the same resolution and subtitle area

- Remove watermark text / Replace specific text:
> If specific text appears in the video that needs to be removed or replaced, edit the ``backend/configs/typoMap.json`` file and add your replacements

```json
{
	"l'm": "I'm",
	"l just": "I just",
	"Let'sqo": "Let's go",
	"Iife": "life",
	"威筋": "威胁",
  	"性感荷官在线发牌": ""
}
```

> This will replace all occurrences of "威筋" with "威胁" and delete all "性感荷官在线发牌" text

- Spaces and Unicode in video paths are supported by the shell-free CLI and covered by regression tests.
- New CPU one-click packages include Python, curated inference libraries, models and VSF. Customers do not install Python or dependencies. Run `Start-VSE.bat` on Windows, open the `.app` on macOS, or run `Start-VSE.sh` on Linux. See [release instructions](docs/release-and-ci.md) for tested OS limits and validation status.

**Downloads**: [this fork's releases](https://github.com/eritpchy/video-subtitle-extractor/releases). Historical assets do not certify the new PP-OCRv6 changes; check the exact release commit and completed CI.

**Recognition modes**:
- Fast: PP-OCRv6 small for supported languages, VideoSubFinder frame detection.
- Auto: small on CPU, medium on an available Paddle CUDA device.
- Accurate: medium models; CPU retains VideoSubFinder detection, compatible GPU configurations can use frame-by-frame detection.
- Unsupported v6 languages retain explicit v5/v3 language recognizers. See [model coverage](docs/PP-OCRv6.md).

The supported release path is CPU. Old DirectML/ONNX/GPU download recommendations do not apply to this version.

## Demo

- GUI version: [Click to view GPU version source code installation tutorial](https://www.bilibili.com/video/bv11L4y1Y7Tj)

<p style="text-align:center;"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/demo.gif" alt="demo.gif"/></p>


## Source Code Usage Guide

#### 1. Install Python

Make sure you have Python 3.12+ installed.

- Windows users can download and install Python from the [Python official website](https://www.python.org/downloads/windows/)
- MacOS users can install via Homebrew:
  ```shell
  brew install python@3.12
  ```
- Linux users can install via package manager, e.g. Ubuntu/Debian:
  ```shell
  sudo apt update && sudo apt install python3.12 python3.12-venv python3.12-dev
  ```

#### 2. Install Dependencies

Use a virtual environment to manage project dependencies and avoid conflicts with the system environment.

(1) Create and activate a virtual environment
```shell
python -m venv videoEnv
```

- Windows:
```shell
videoEnv\\Scripts\\activate
```
- MacOS/Linux:
```shell
source videoEnv/bin/activate
```

#### 3. Navigate to the Project Directory

Switch to the source code directory:
```shell
cd <source-code-directory>
```
> For example: if your source code is in the tools folder on drive D, and the source code folder is named video-subtitle-extractor, enter:
> ```shell
> cd D:/tools/video-subtitle-extractor-main
> ```

#### 4. Install the Current CPU Runtime

The maintained/tested dependency set is native Python 3.12 with the Paddle CPU
engine. DirectML, ONNX Runtime providers, CoreML and ROCm are **not inference
backends in this version**. The old accelerator recipes and
`requirements_directml.txt` are legacy and do not enable acceleration here.
CUDA may work with a separately compatible Paddle GPU installation, but is not
certified by the CPU release matrix; do not install CPU and GPU Paddle packages
together.

```shell
python -m pip install -r requirements.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.5
python -m backend.tools.model_download --tiers small medium
```

Use [the current runtime guide](docs/USAGE.md) and
[PP-OCRv6 model documentation](docs/PP-OCRv6.md) for supported models, offline
operation and hardware behavior.

#### 5. Run the Program

- Run the GUI version:

```shell
python gui.py
```

- Run the CLI version:

```shell
python -m backend.cli "input.mp4" --language en --cpu
```

## FAQ

#### 1. Program not working / no results / CUDA and cuDNN issues

Start with the CPU setup above. Check `python -m pip check`, model verification, the VSF installer verification and the extraction log. An OCR error now returns a nonzero exit status rather than a successful empty result.

#### 2. 7z file extraction error

Solution: Upgrade 7-Zip to the latest version.

## Sponsor
<img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/sponsor.png" width="600">
