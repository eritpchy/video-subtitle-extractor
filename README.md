> 本分支更新：支持真实 PP-OCRv6 small/medium、VideoSubFinder CLI v6.10.7、日间/夜间/跟随系统主题和无交互 CLI。安装、Docker 与测试说明见 [当前使用指南](docs/USAGE.md)。发布与跨平台验证状态以对应提交的 GitHub Actions 结果为准。

简体中文 | [English](README_en.md)

<p align="center"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/icon_1024.png" alt="VSE Logo" width="128"/></p>

## 项目简介

![License](https://img.shields.io/badge/License-Apache%202-red.svg)
![python version](https://img.shields.io/badge/Python-3.12-blue.svg)
![support os](https://img.shields.io/badge/OS-Windows/macOS/Linux-green.svg)  

Video-subtitle-extractor (VSE) 是一款将视频中的硬字幕提取为外挂字幕文件(srt格式)的软件
主要实现了以下功能：

- 提取视频中的关键帧
- 检测视频帧中文本的所在位置
- 识别视频帧中文本的内容
- 过滤非字幕区域的文本
- 去除水印、台标文本、原视频硬字幕，可配合：[video-subtitle-remover (VSR) ](https://github.com/YaoFANGUK/video-subtitle-remover/tree/main)
- 去除重复字幕行，生成srt字幕文件/txt文本文件
- 支持视频字幕**批量提取**
- 多语言：支持**简体中文（中英双语）**、**繁体中文**、**英文**、**日语**、**韩语**、**越南语**、**阿拉伯语**、**法语**、**德语**、**俄语**、**西班牙语**、**葡萄牙语**、**意大利语**等**87种**语言的字幕提取
- 多模式：
  - **快速**：（推荐）v6 支持语言使用 small 模型，兼容语言保留专用识别器。
  - **自动**：只有启用且可用的 Paddle CUDA 使用 medium；CPU、DirectML、Python/Native CoreML 使用 small。
  - **精准**：使用 medium 检测/适用的识别模型；仍可能漏字或误识别，速度取决于后端、视频与硬件。

> 请优先使用快速/自动模式，如果前两种模式存在较多丢字幕轴情况时，再使用精准模式
 
<p style="text-align:center;"><img src="design/demo.png" alt="demo.png"/></p>

**项目特色**：

- 采用本地进行OCR识别，无需设置调用任何API，不需要接入百度、阿里等在线OCR服务即可本地完成文本识别
- 支持显式后端加速；速度须实际测量。同一模式/模型不会因为换成 GPU 就自动获得更高准确率。

**使用说明**：

- 有使用问题请加群讨论，QQ群：210150985（已满）、806152575（已满）、816881808（已满）、295894827

- 点击【打开】后选择视频文件，调整字幕区域，点击【运行】
  - 单文件提取：打开文件的时候选择**单个**视频
  - **批量提取**：打开文件的时候选择**多个**视频，确保每个视频的分辨率、字幕区域保持一致

- 去除水印文本/替换特定文本：
> 如果视频中出现特定的文本需要删除，或者特定的文本需要替换，可以编辑 ``backend/configs/typoMap.json``文件，加入你要替换或去除的内容

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

> 这样就可以把文本中出现的所有“威筋”替换为“威胁”，所有的“性感荷官在线发牌”文本删除

- 新的无 shell CLI 支持视频路径中的空格与中文，相关行为已有回归测试。
- 新版 CPU 一键包内置 Python、精简推理环境、模型和 VSF，客户无需安装 Python 或依赖。Windows 运行 `Start-VSE.bat`，macOS 打开应用 `.app`，Linux 运行 `Start-VSE.sh`。具体系统支持范围及验收状态见[发布说明](docs/release-and-ci.md)。

**下载地址**：[本分支 Releases](https://github.com/eritpchy/video-subtitle-extractor/releases)。历史附件不代表已包含新的 PP-OCRv6 改动，请核对发布提交与对应 CI 结果。

**识别模式**：
- 快速：支持语言使用 PP-OCRv6 small，VideoSubFinder 提取关键帧。
- 自动：仅启用且可用的 Paddle CUDA 使用 medium；CPU、DirectML 和 CoreML 保持 small。
- 精准：使用 medium 模型；CPU/CoreML 保持 VideoSubFinder 检测，Paddle CUDA/DirectML 可使用逐帧检测。
- v6 未覆盖的语言明确回退到 v5/v3 专用识别模型，详见[模型覆盖说明](docs/PP-OCRv6.md)。

源码提供默认 Paddle、Windows DirectML 和 macOS Python CoreML 路径；一键包的 Native 实现与源码安装独立。可用后端不等于已完成硬件验收，请以对应提交的实际 CI 证据为准。

## 演示

- GUI版：[点击查看GPU版本源码运行的安装教程 👈](https://www.bilibili.com/video/bv11L4y1Y7Tj)

<p style="text-align:center;"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/demo.gif" alt="demo.gif"/></p>


## 源码使用说明

**源码应用可直接用 Python 运行，不要求编译或调用本项目的 C++ `native-ocr` worker。**
GUI/CLI、视频调度及 Paddle、DirectML、Python CoreML 的 OCR 适配与处理代码均保留在仓库。
`native/ocr/` 的 C++ worker 是独立的一键包实现，不是运行 Python 源码的前置条件。
这不意味着所有第三方依赖都是纯 Python：Qt/PySide、Paddle、ONNX Runtime、OpenCV、FFmpeg 和
VideoSubFinder 含原生二进制，仍需按所选平台安装其受支持的发行包。

### 1. 干净克隆与 Python 环境

使用 **Python 3.12**（不是任意“3.12+”），每个后端创建独立虚拟环境，不混装 Paddle CPU/GPU
或不同 ONNX Runtime 发行包。

```sh
git clone https://github.com/eritpchy/video-subtitle-extractor.git
cd video-subtitle-extractor
python3.12 -m venv .venv
. .venv/bin/activate
```

Windows PowerShell 将后两行替换为：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
```

### macOS GUI 的 Qt 准备边界

本候选要求 **macOS arm64 26.0+**，以全部原生库的实际最低版本为准。Qt/PySide 固定
6.12.0；已识别的 Cocoa 辅助功能问题使用单插件候选修补，不能以普通 GUI 启动冒充外部 AX 验收。
所有 macOS 源码后端共用此 Qt 准备，不依赖本项目 C++ worker 或编译器。

在用户自己创建的 venv 安装所选依赖时，可显式加 `-c requirements-macos-qt.txt`。
随后关闭使用该 venv 的 GUI，先运行 `python scripts/prepare_macos_qt.py` 只读检查。
**当前公共修复资源尚未发布，生产准入仍禁止**；仅已授权候选测试使用同一固定本地资源：

```sh
python scripts/prepare_macos_qt.py --apply --candidate-testing --archive /absolute/path/vse-qt6.12.0-macos-arm64-ax772484-ps6.zip
```

它只改当前隔离 venv 的一个 Qt 插件，保留原 wheel RECORD、许可、源码及独立收据，不改系统环境。
未来修复版公开资源和完整验收通过后才可使用 `--apply` 的固定公开下载路径；详见
[Qt 候选来源、完整对应源码及仍待验收项](docs/audits/macos-qt-overlay.md)。

### 2. 默认 Paddle Python 路径（CPU）

```sh
python -m pip install -r requirements.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.7
python -S scripts/prepare_source_models.py paddle --output build/source-paddle-models --cache build/model-downloads/paddle
python -S scripts/prepare_source_models.py verify --format paddle build/source-paddle-models
```

这会准备 **全部 15 个固定原模型**：4 个 v6 small/medium det/rec、10 个 v5 兼容识别器、
1 个 v3 Kannada 识别器；不再只下载四个 v6 模型。输出必须是新目录，不覆盖 clone 自带模型。
运行前指定独立数据根：

```sh
export VSE_OCR_BACKEND=paddle
export VSE_MODEL_DIR="$PWD/build/source-paddle-models"
python gui.py
# 或：
python -m backend.cli "input.mp4" --language en --mode fast --cpu
```

PowerShell 环境变量等价写法：

```powershell
$env:VSE_OCR_BACKEND = 'paddle'
$env:VSE_MODEL_DIR = "$PWD/build/source-paddle-models"
python gui.py
```

### 3. Windows Python CUDA 11.8 / 12.6 源码路径

在全新 Windows x64 / Python 3.12 虚拟环境中，选择
`requirements-source-cuda118.txt` **或** `requirements-source-cuda126.txt`，不要与 CPU Paddle
或另一 CUDA 变体混装。这两条路径使用完整 PySide6 和普通 pip，不运行自研 C++ worker。
系统仍需兼容的 NVIDIA 驱动；CUDA wheel/运行库不包含系统显卡驱动。
完整环境创建、安装、`pip check`、设备探测、全 15 模型与 CLI 命令见
[Windows CUDA 源码安装步骤](docs/USAGE.md#windows-python-cuda-118-or-126-source-backend)。
目前的 Windows Server 包验收是明确 CPU 推理证据，不代表这些新源码安装步骤或真实 CUDA 硬件已验收。
CUDA 10.2 仅保留旧 2.2.0 下载，不属于当前 Python 3.12 / Paddle 3.3.1 源码支持。

### 4. DirectML / Python CoreML 源码路径

在另一个干净 Python 3.12 虚拟环境中选择其一：

| 平台/后端 | 依赖文件 | 后端变量 | 模型目录变量 |
| --- | --- | --- | --- |
| Windows DirectML | `requirements-directml-inference.txt` | `VSE_OCR_BACKEND=directml` | `VSE_DIRECTML_MODEL_ROOT` |
| macOS arm64 Python CoreML | `requirements-coreml-inference.txt` | `VSE_OCR_BACKEND=python-coreml` | `VSE_PYTHON_COREML_MODEL_ROOT` |

安装对应依赖并运行 VSF 安装脚本后，准备共同的全 15 模型 FP32 ONNX 数据：

```sh
python -S scripts/prepare_source_models.py onnx --output build/source-onnx-models --cache build/model-downloads/onnx
python -S scripts/prepare_source_models.py verify --format onnx build/source-onnx-models
```

模型来源为公开、固定 SHA256 的正式 2.3.0 ZIP。工具**只提取 15 个 graph、13 个 config 和两个
模型清单**；不提取、安装或执行其中的 Linux worker/runtime。无需 GitHub 登录、Actions token、
C++ 编译器或本机模型转换器。已有原 ZIP 可用 `--archive PATH --offline` 代替下载。

例如 macOS（Windows DirectML 的 PowerShell 写法见[使用指南](docs/USAGE.md)）：

```sh
export VSE_OCR_BACKEND=python-coreml
export VSE_PYTHON_COREML_MODEL_ROOT="$PWD/build/source-onnx-models"
python gui.py
# 同一 Python 路径显式使用 CPU：
python -m backend.cli "input.mp4" --language en --mode accurate --cpu
```

后端注册、模型校验或旧 Native worker 的结果都不能替代这个 Python 后端的真实推理验收。
CoreML EP 也不等于已证明物理 GPU 执行/内部精度。详见[完整源码、离线与可复现转换说明](docs/USAGE.md)。

## 常见问题与解决方案

#### 1. 运行不正常/没有结果/cuda及cudnn问题

请先使用以上 CPU 安装步骤，检查 `python -m pip check`、模型校验、VSF 安装校验和提取日志。OCR 出错时会返回非零退出码，不再把失败当作成功的空字幕。

#### 2. 7z文件解压错误

解决方案：升级7-zip解压程序到最新版本

## 赞助
<img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/sponsor.png" width="600">

