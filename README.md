> 本分支更新：支持真实 PP-OCRv6 small/medium、VideoSubFinder CLI v6.10、日间/夜间/跟随系统主题和无交互 CLI。安装、Docker 与测试说明见 [当前使用指南](docs/USAGE.md)。发布与跨平台验证状态以对应提交的 GitHub Actions 结果为准。

简体中文 | [English](README_en.md)

<p align="center"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/icon_1024.png" alt="VSE Logo" width="128"/></p>

## 项目简介

![License](https://img.shields.io/badge/License-Apache%202-red.svg)
![python version](https://img.shields.io/badge/Python-3.12+-blue.svg)
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
  - **快速**：（推荐）使用轻量模型，快速提取字幕，可能丢少量字幕、存在少量错别字
  - **自动**：（推荐）自动判断模型，CPU下使用轻量模型；GPU下使用精准模型，提取字幕速度较慢，可能丢少量字幕、几乎不存在错别字
  - **精准**：（不推荐）使用精准模型，GPU下逐帧检测，不丢字幕，几乎不存在错别字，但速度**非常慢**

> 请优先使用快速/自动模式，如果前两种模式存在较多丢字幕轴情况时，再使用精准模式
 
<p style="text-align:center;"><img src="design/demo.png" alt="demo.png"/></p>

**项目特色**：

- 采用本地进行OCR识别，无需设置调用任何API，不需要接入百度、阿里等在线OCR服务即可本地完成文本识别
- 支持GPU加速，GPU加速后可以获得更高的准确率与更快的提取速度

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
- 自动：CPU 使用 small，可用 Paddle CUDA 设备使用 medium。
- 精准：使用 medium 模型；CPU 保持 VideoSubFinder 检测，兼容的 GPU 配置可使用逐帧检测。
- v6 未覆盖的语言明确回退到 v5/v3 专用识别模型，详见[模型覆盖说明](docs/PP-OCRv6.md)。

当前发布验证范围为 CPU，旧的 DirectML/ONNX/GPU 安装和下载建议不适用于本版本。

## 演示

- GUI版：[点击查看GPU版本源码运行的安装教程 👈](https://www.bilibili.com/video/bv11L4y1Y7Tj)

<p style="text-align:center;"><img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/demo.gif" alt="demo.gif"/></p>


## 源码使用说明

#### 1. 安装 Python

请确保您已经安装了 Python 3.12+

- Windows 用户可以前往 [Python 官网](https://www.python.org/downloads/windows/) 下载并安装 Python
- MacOS 用户可以使用 Homebrew 安装：
  ```shell
  brew install python@3.12
  ```
- Linux 用户可以使用包管理器安装，例如 Ubuntu/Debian：
  ```shell
  sudo apt update && sudo apt install python3.12 python3.12-venv python3.12-dev
  ```

#### 2. 安装依赖文件

请使用虚拟环境来管理项目依赖，避免与系统环境冲突

（1）创建虚拟环境并激活
```shell
python -m venv videoEnv
```

- Windows：
```shell
videoEnv\\Scripts\\activate
```
- MacOS/Linux：
```shell
source videoEnv/bin/activate
```

#### 3. 创建并激活项目目录

切换到源码所在目录：
```shell
cd <源码所在目录>
```
> 例如：如果您的源代码放在 D 盘的 tools 文件夹下，并且源代码的文件夹名为 video-subtitle-extractor，则输入：
> ```shell
> cd D:/tools/video-subtitle-extractor-main
> ```

#### 4. 安装当前 CPU 运行环境

当前维护和测试的依赖组合为原生 Python 3.12 与 Paddle CPU 推理引擎。
本版本**不使用 DirectML、ONNX Runtime、CoreML 或 ROCm 作为推理后端**；
旧的加速安装教程和 `requirements_directml.txt` 仅为历史遗留内容，安装它们不会启用加速。
CUDA 需要另外匹配的 Paddle GPU 环境，目前不属于 CPU 发布矩阵的验证范围，
请勿将 Paddle CPU 与 GPU 软件包同时安装。

```shell
python -m pip install -r requirements.txt
python -m pip check
python scripts/install_videosubfinder.py --version 6.10
python -m backend.tools.model_download --tiers small medium
```

支持的模型、离线部署和硬件行为请参阅[当前使用指南](docs/USAGE.md)及
[PP-OCRv6 模型说明](docs/PP-OCRv6.md)。

#### 5. 运行程序

- 运行图形化界面版本（GUI）

```shell
python gui.py
```

- 运行命令行版本（CLI）

```shell
python -m backend.cli "input.mp4" --language en --cpu
```

## 常见问题与解决方案

#### 1. 运行不正常/没有结果/cuda及cudnn问题

请先使用以上 CPU 安装步骤，检查 `python -m pip check`、模型校验、VSF 安装校验和提取日志。OCR 出错时会返回非零退出码，不再把失败当作成功的空字幕。

#### 2. 7z文件解压错误

解决方案：升级7-zip解压程序到最新版本

## 赞助
<img src="https://github.com/YaoFANGUK/video-subtitle-extractor/raw/main/design/sponsor.png" width="600">

