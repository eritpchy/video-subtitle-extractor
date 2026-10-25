# CUDA distribution audit — 2026-10-25

**Status: measured candidate, NOT a certified slim GPU release.** No GPU device or
NVIDIA driver is available in this cloud workspace. CPU execution, CUDA compilation,
and importing a CUDA wheel are not substitutes for real CUDA OCR execution.

## Actual size, not small-file guesses

Official Paddle 3.3.1 `cp312` CUDA 12.6 wheels exist for Linux x86-64 and Windows
x86-64 in [Paddle's index](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/).
Remote ZIP central directories and METADATA were read without installing the full
GPU environment. NVIDIA dependency sizes and SHA256 values were obtained from
PyPI's versioned JSON metadata and wheel directories. Detailed inventory is in
`cuda-wheel-inventory-2026-10-25.json`.

| Component | Linux bytes | Windows bytes |
|---|---:|---:|
| Paddle wheel, compressed | 2,021,398,367 | 579,382,604 |
| Paddle, expanded | 3,506,073,785 | 1,111,356,998 |
| Paddle + directly declared NVIDIA wheels, compressed | 4,032,276,517 | 2,198,391,682 |
| Paddle + directly declared NVIDIA payload, expanded | 6,579,080,777 | 3,457,552,883 |

These are direct-dependency wheel payload measurements, **not a fully resolved dependency closure or final dist sizes**: Qt, Python, models,
application, FFmpeg, native dependency closure and filesystem overhead are extra.
The CUDA 12.6 METADATA pins cuDNN 9.5.1.17, cuBLAS 12.6.4.1 and runtime 12.6.77.
Linux declares 15 NVIDIA packages; Windows declares seven.

The Linux core is already stripped (no `.symtab` or debug sections in libpaddle,
phi_core or phi_gpu). Its largest libraries are phi_core 1,009,012,016 bytes,
phi_gpu 812,326,176 bytes, libpaddle 500,742,632 bytes. Blind `strip` cannot solve
this. cuBLAS Lt alone is 491,106,832 bytes; cuDNN's precompiled engines are
438,317,848 bytes. They are not safely removable just because this app does inference.

## Mandatory dependencies versus candidates

Six core ELF files were fetched selectively and their SHA256 values verified
against the official wheel's RECORD. `cuda-elf-dependencies-2026-10-25.json`
records their exact DT_NEEDED. Paddle's index does not publish a whole-wheel
SHA256 anchor; this entry-level verification is not described as such an anchor.

`libpaddle.so` and `libcinnapi.so` directly require cuBLAS, cuDNN, cuRAND,
cuSolver, NVRTC, cudart, nvToolsExt, phi_core/phi_gpu, oneDNN and MKL. Their
transitive dependencies must also remain. `libcuda.so.1` is the **OS NVIDIA
driver**, not a toolkit library to copy from build-machine stubs.

Potential Linux-only reductions, pending actual GPU workload verification:

* `libflashattn.so`, `libflashattnv3.so`, `libflashmaskv2.so`: **818,691,336 bytes**.
  Not direct DT_NEEDED of the six audited core ELFs; upstream loads them with
  dlopen wrappers. All four pinned PP-OCRv6 PIR graphs are FP32, without flash
  or distributed operators. PaddleX 3.7.2's `paddle_static/runner.py` explicitly
  chooses `PrecisionType.Float32` for the normal `paddle` run mode. Crucially,
  Paddle's default PIR GPU passes *do* include `fused_flash_attn_pass`; its
  pattern guards reject FP32 unless GPU mixed precision is enabled. Registered
  FlashAttention/FlashMask GPU kernels only accept FP16/BF16.
* NCCL: 271,912,285 bytes total package. No collectives in these pinned graphs;
  dynamic loading exists upstream. Import-time/distributed initialization still
  needs real-workload verification. Do not remove based solely on the name.
* CUPTI: 34,855,781 bytes; cuFile: 3,179,200 bytes; CCCL: 12,547,502 bytes.
  Profiling, GPU-direct storage and development headers are outside the proposed
  profile, but absence from our direct imports is not a complete dlopen trace.

No candidate above has been deleted from a delivered runtime. No duplicate CUDA
library removal is claimed: no duplicate-content audit of an installed GPU
runtime has been completed. In particular, preserve cuDNN's dynamically selected
engines and cuBLAS Lt. Windows has **no matching FlashAttention payload** in its
Paddle wheel, so Linux's potential saving must not be promised for Windows.

Sources at the exact Paddle v3.3.1 tag:
[dynamic loader](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/paddle/phi/backends/dynload/dynamic_loader.cc),
[FlashAttention kernels](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/paddle/phi/kernels/gpu/flash_attn_kernel.cu),
[fusion guards](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/paddle/fluid/pir/transforms/gpu/fused_flash_attn_pass.cc),
[GPU pass list](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/paddle/fluid/inference/api/paddle_pass_builder.cc).

## Proposed profile and release gates

Use the same bundled-Python one-click architecture as the CPU release, but a
separate GPU dependency lock/profile and artifact. Never install both CPU and
GPU Paddle distributions into one runtime. The customer must not install Python,
pip, CUDA toolkit, or cuDNN; compatible NVIDIA hardware and OS driver remain
necessary prerequisites. Do not ship driver stubs as if they were working drivers.

A future slim profile must restrict execution to pinned PP-OCRv6 models, one GPU,
FP32, Paddle engine. It must reject custom models, FP16/BF16/AMP, TensorRT/HPI,
distributed training and profiling rather than silently run unsupported features.
V5/V3 language fallbacks have not been certified by this V6-only audit; either
retain their full runtime or audit/test them before extending the profile.

`python scripts/cuda_profile_audit.py --models backend/models/V6` validates the
pinned graph hashes and records static operations/types. `--gpu-probe` additionally
requires a real GPU and executes/synchronizes an FP32 matrix multiply. It fails
closed on CPU/no device. **Even a successful tensor probe does not mark OCR E2E
as passed.** This helper does not modify the production device-selection policy.

Certification still requires a real NVIDIA host, no toolkit/Python installed,
network-disabled/read-only final bundle, actual small/medium detection and
recognition, supported-language fixtures and full subtitle video/timeline E2E,
loaded-library traces, and a before/after baseline comparison. Run baseline and
candidate on identical hardware. Missing GPU is a blocker, not a skip/pass.

Windows also needs explicit verification of the upstream-reported
[cuDNN compiled 9.9 versus packaged 9.5 warning](https://github.com/PaddlePaddle/Paddle/issues/79388).
That report says OCR succeeded but does not establish compatibility for every
workload. Do not silently override the vendor pin or suppress its warning.

## Inference-only rebuild feasibility

The official source supports `ON_INFER`, `WITH_PYTHON`, `WITH_DISTRIBUTE`,
`WITH_PROFILER`, `WITH_CINN`, `WITH_FLASHATTN` and `WITH_FLASHATTN_V3` build
controls. `cmake/inference_lib.cmake` explicitly copies FlashAttention only when
enabled. A custom pinned inference-oriented build can avoid linking unused
features in the first place; it is safer than deleting a hard dependency.
However, the C++ `paddle_inference` library is **not a drop-in replacement** for
PaddleX's Python `paddle.inference`/`libpaddle` API. No current official cp312
inference-only wheel with verified PP-OCRv6 parity was established in this audit.
Do not install unrelated third-party reduced wheels or claim a hypothetical
custom-build size. Building one requires a new reproducible toolchain, artifact
provenance, a supported GPU architecture matrix and the same GPU E2E gates.

[Build options](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/CMakeLists.txt),
[inference-library assembly](https://github.com/PaddlePaddle/Paddle/blob/v3.3.1/cmake/inference_lib.cmake).

## VSF CUDA is a separate distribution

Read-only inspection of fork v6.10's existing CUDA12 artifact measured
134,633,200 compressed bytes and 317,380,137 expanded bytes, with 230 library
entries. NPP ig/icc total about 49 MB. Ubuntu OpenCV imgcodecs pulls GDAL/GDCM,
which brings unrelated GIS/database/image codecs: ICU data 29,479,648 bytes,
GDAL 23,150,120, spatialite 8,304,656, MySQL 6,859,352, plus others.
These are DT_NEEDED, so removal without relinking would break the binary.
The correct fix is rebuilding minimal OpenCV/FFmpeg, keeping required decoding
including dav1d AV1, and retaining genuine CUDA NPP functionality. The separate
VSF build worker owns this change. Existing CUDA CI compiles in CUDA12.6.3 devel
but does not by itself prove real GPU execution.


## Existing Windows CUDA 12.6 release: bounded header audit

The untouched `2.2.0` release's two volumes total 3,751,414,321 bytes.
Only **130,065 bytes** of HTTP Range data were fetched to parse the 7z headers;
no payload was extracted. Headers list 5,592 entries and 4,107,800,590 expanded
bytes. See `old-windows-cuda126-summary-2026-10-25.json`.

* `opt/packages`: 2,833,501,875 bytes, mainly offline wheels; embedded Python is
  88,951,550 bytes. This does **not** prove a duplicate full installed environment
  plus wheelhouse—the old package deploys from these wheels.
* Test videos: 344,145,024 bytes; design samples/PDF/GIFs: 48,397,279 bytes.
* All models: 536,912,378 bytes, including legacy V2/V4. These cannot all simply
  be removed without matching the supported-language/model policy.
* VSF: 238,084,770 bytes including **47,123,092 bytes of Linux binaries in a
  Windows release**. Windows VSF itself is 190,961,678 bytes.
* Three conflicting OpenCV distributions: contrib 46,185,163 bytes, regular
  39,488,044 bytes, headless 39,402,386 bytes. A clean exact closure avoids this.
* Multiple wheels of NumPy, Pillow, NetworkX, protobuf and certifi are present.
  NumPy has versions 2.2.4 and 2.3.1; Pillow has three versions.
* PySide6 Addons wheel: 143,150,906 bytes; an Essentials-only native profile avoids
  carrying WebEngine/unused GUI modules.
* CUDA12.6 release carries an NVJitLink **12.8.93** wheel of 268,553,710 bytes.
  It must be traced to transitive vendor requirements before proposing removal.

The historical workflow installs/freeze-captures an environment, packages with
QPT, then downloads requirements again into `opt/packages` and copies them into
Release. That explains version duplication. Its 10.2 branch explicitly invokes
QPT `CopyCUDAPackage`; 11.8/12.6 use Paddle GPU wheels. This read-only investigation
does not modify old assets or imply new PP-OCRv6 support for old CUDA10.2 binaries.

## Implemented large Windows development-archive reduction

NVIDIA's cuFFT/cuSolver/cuSparse wheels **transitively require NVJitLink**. Unlike
the Linux Paddle wheel, the Windows Paddle wheel does not pin that package.
Therefore the direct Windows total above excludes a required package and must
not be presented as a complete recursive closure.

The official Windows NVJitLink 12.6.85 wheel is 161,908,572 bytes and expands to
492,874,196 bytes. Its **static development archive alone is 453,528,212 bytes**;
the runtime DLL is 39,236,608 bytes. The old release's 12.8.93 wheel expands to
1,079,877,460 bytes, including a 1,001,900,552-byte static archive and a
77,860,352-byte runtime DLL. These `.lib` static archives are linker inputs, not
runtime DLLs. Preserve the DLL, import library, header, licenses and metadata.

`scripts/prune_cuda_development.py` implements a separate, default-dry-run,
fail-closed rule for those two exact official Windows x64 versions. It checks
metadata, the DLL's and archive's exact SHA256/size, the static-archive signature,
required supporting files, and symlink safety before any deletion. `--apply`
writes a prepared/applied audit record and removes **only** the static archive.
Unknown versions/corrupt or missing DLLs are rejected. This rule is for a frozen
inference application, not a customer C++/CUDA extension build environment.

Actual validation on the complete official **12.6.85** wheel:

* Whole wheel SHA256 verified against PyPI:
  `e61120e52ed675747825cdd16febc6a0730537451d867ee58bee3853b1b13d1c`.
* Before: 492,874,196 payload bytes.
* After: 39,346,521 bytes, including the new audit file.
* Removed: **453,528,212 bytes**; retained runtime DLL remains hash-identical.
* 12.8.93's hashes/sizes were inspected remotely; it was not fully downloaded or
  pruned in this experiment. Its larger saving is supported inventory, not an
  executed full-package test.

With an explicit Windows NVJitLink12.6.85 pin (still requiring native GPU
compatibility testing), Paddle + recursive NVIDIA package payload is
3,950,427,079 bytes; this one rule reduces it to 3,496,898,867 bytes before the
audit-file overhead. No full application/dist size or real GPU success is claimed.

See `windows-nvjitlink-development-2026-10-25.json` for official download URLs,
whole-wheel hashes, per-entry sizes and the actual prune report. Tests for the
CUDA graph/probe helper and static-archive pruner pass; a real invocation of
`--gpu-probe` on this CPU cloud host correctly exits nonzero rather than treating
CPU execution as CUDA success. Main CPU packaging/pruning files are untouched.
