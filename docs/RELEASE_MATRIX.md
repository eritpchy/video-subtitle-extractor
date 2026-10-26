# Automatic self-contained package candidates

## Scope and release safety

`release.yml` now builds on branch changes and pull requests, and can also be
started manually. It **only uploads CI artifacts**. It cannot create a release,
move a tag, overwrite an existing asset, or change the application version.
Every candidate records its actual source commit, not the already published
2.3.0 commit. Publishing a later approved version is a separate operation.

**Required Mac acceleration is still a release blocker.** The final new Mac
package must have an honest `native-coreml` identity, actual CoreML/GPU evidence,
and an explicit CPU option in the same bundled environment. The successful
seven-profile CPU-acceptance run is a retained baseline, not completion of this
requirement. Existing published 2.3.0 Native CPU assets remain unchanged. Do not
rename a CPU-only bundle or equate a requested CoreML provider/compute plan with
observed GPU execution. The production provider/package integration is now in source; full publication
remains blocked pending real Mac validation; no extra duplicate Mac CPU environment is planned for final
distribution merely to preserve an internal regression baseline.

The new seven-profile candidate set below replaces only the **new** Mac CPU
artifact with one dual-provider CoreML artifact. The successful earlier run is
still a CPU-only baseline, not evidence for this new identity:

| Platform | Package profile | Inference runtime |
| --- | --- | --- |
| Windows x64 | `cpu` | Paddle 3.3.1 CPU |
| Windows x64 | `cuda11.8` | Official Paddle GPU 3.3.1 CUDA 11.8 wheel |
| Windows x64 | `cuda12.6` | Official Paddle GPU 3.3.1 CUDA 12.6 wheel |
| Windows x64 | `directml` | Original ONNX Runtime DirectML 1.20.1 / DirectML 1.15.2 |
| Windows x64 | `native-cpu` | Existing native CPU worker |
| Linux x64 | `native-cpu` | Existing native CPU worker |
| macOS arm64 | `native-coreml` | Existing pinned ORT 1.31.0; explicit CoreML CPU+GPU and CPU in one package |

Windows builds and package acceptance run on **Server 2022 and Server 2025**;
only the Server 2022 build of each profile is selected for the canonical set.
This is not Windows 10/11 or real GPU certification. Native CPU still runs its
existing model/worker tests; this workflow does not replace GPU packages with it.
VSF remains pinned to 6.10.7.

The current Windows automated launcher rejects percent-containing command paths;
use a directory without `%`. Unicode, spaces and `&!()` remain in its supported
validation contract. The recovery tool's independent `&!%()` path coverage does
not broaden the BAT launcher's acceptance or certify arbitrary client Windows
versions. The accepted 306 baseline is not modified to hide this boundary.

**macOS requirement correction:** the reviewed full Qt 6.12.0 payload declares
an actual maximum Mach-O minimum of **macOS 26.0**, not 14.0/14.4. In particular,
Apple's own `otool -arch arm64 -l` reports `LC_BUILD_VERSION minos 26.0` for
`PySide6/QtCore.abi3.so` (SHA256
`1288c120342b8460a8829856a07abf2d7d63be8f72502c3dcffc146bb780cbec`).
This is `minos`, not the separate SDK field; 28 original PySide/Shiboken images
limit the complete payload. The candidate's Info.plist follows the actual
whole-package scan. New Mac CI uses the official GA `macos-26` arm64 runner,
including the real app-launcher gate. The old published ZIP and its inaccurate
14.0 declaration are not rewritten. Direct Python execution on macOS 15 does
not certify Finder/app-launch compatibility or lower the declared requirement.
See [GitHub's runner announcement](https://github.blog/changelog/2026-02-26-macos-26-is-now-generally-available-for-github-hosted-runners/).

DirectML waits for the Native workflow to finish, but an unrelated Native test
failure does not suppress its independent diagnostic evidence. Its converted
models must come from the same workflow run and exact source-SHA artifact, and
every pinned graph/config must still verify. Missing/corrupt models fail closed.
The aggregate candidate-set job still requires all profile jobs to succeed.

All packages include their interpreter, selected inference runtime, media tools,
GUI, and all supported language/model assets. DirectML ships all 15 preconverted
FP32 models and their configuration/dictionaries, not a first-launch converter.
Paddle packages likewise stage all 15 original, pinned models (45 source files),
including Tamil, Telugu and Kannada; an incomplete tracked model tree fails
closed instead of initiating a customer-side model download.
Customers do not install Python, Paddle, ONNX Runtime, CUDA Toolkit or cuDNN.
An appropriate supported OS and GPU driver remain platform prerequisites.

## Historical CUDA 10.2 compatibility

The most recent old release, 2.2.0, had five Windows profiles: CPU, DirectML,
CUDA 10.2, CUDA 11.8 and CUDA 12.6. Its CUDA 10.2 package used Python 3.11,
Paddle 2.5.2.post102 and NumPy 1.26.4. That older Paddle/PaddleOCR API cannot
simply execute the new PaddleOCR 3.7 / PP-OCRv6 PIR pipeline.

The approved migration retains the immutable [2.2.0 CUDA 10.2 download](https://github.com/eritpchy/video-subtitle-extractor/releases/tag/2.2.0).
It does **not** claim that the old download supports the new models, nor produce
a CPU/CUDA 11.8 archive under a CUDA 10.2 filename. CUDA 12.8, which was absent
from 2.2.0, is not silently reintroduced.

The official Windows cp312 Paddle 3.3.1 CUDA 11.8 wheel records compiled GPU
architectures `[61, 70, 75, 80, 86]`; CUDA 12.6 records `[75, 80, 86, 89]`.
Thus CUDA 11.8 is the explicit **candidate** path for GTX 10-series SM61 users,
not CUDA 12.6. Compiled architecture inclusion is not real GPU OCR validation.
Sources: [official cu118 index](https://www.paddlepaddle.org.cn/packages/stable/cu118/paddlepaddle-gpu/),
[official cu126 index](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/),
[NVIDIA legacy GPU table](https://developer.nvidia.com/cuda/gpus/legacy).

The vendor wheels also record compiled cuDNN 8.9.6 / 9.9.0 while declaring
runtime distributions 8.9.4.19 / 9.5.1.17 respectively. The build retains the
vendor's dependency pins and records both facts; it does not suppress warnings
or invent a newer compatible DLL set.

## Acceptance and size controls

Before a candidate can be uploaded, its final ZIP is extracted and launched
through the real included launcher. Gates cover checksum/inventory, GUI,
fast/accurate/auto **explicit CPU** video-to-SRT, multiline/AV1/timeline
regressions, loaded-library provenance, and both clean-PATH and simulated
Conda/venv/Qt/Python/native-path contamination. These checks run on final ZIP
bytes, not merely the build tree. The Linux Native package additionally retains
its clean offline OS-container gate.

Paddle CPU/CUDA final launchers additionally run 168 frozen **CPU** image cases:
56 per actual `fast`/`accurate`/`auto` call, covering all 15 models, all 11 legacy
recognizers and the Kannada aliases, plus standalone detection. CPU auto selects
the small tier; accurate selects medium. Fixed test images/goldens remain on the
build host, while inference executes the extracted ZIP's Python, production
modules and packaged weights. Every case, source pin and model hash must pass;
a source-tree-only report or partial model/mode coverage cannot issue a receipt.
The contamination scenarios retain three-mode video tests without redundantly
repeating the complete image suite. Packaged Paddle always pins VSE_MODEL_DIR
to its own verified weights, rather than borrowing a host model directory.
None of these CPU results certifies a GPU.

Mac `native-coreml` additionally executes the same 168 frozen image cases once
with explicit CPU and once with CoreML (336 actual calls), preserving both raw
native results and production display ordering. All 15 fixed FP32 ONNX graphs,
configs, confidence tolerances and mode choices are unchanged. The final ZIP
worker SHA, actual PNG input SHA, request ID, process ID, inference window,
first-run profile reuse and CPU subgraph evidence are recorded. Blank frames do
not pretend recognition ran. Each of the three real video modes must also run
through the same production VSF route with CoreML and validate real per-request
EP evidence, including both clean and contaminated environments. No all-CPU EP
fallback is silently accepted as CoreML. Explicit `--cpu` and the GUI switch
remain available in the same package.

CoreML EP events and `CPUAndGPU` configuration are **not** physical GPU dispatch
proof, nor proof of CoreML internal numeric precision. Those real-device checks,
the Mac GUI/AX regression and final package evidence are separate release
blockers. Internal CoreML CPU work remains disclosed. No physical GPU pass or
speedup is inferred by these candidate receipts.

The isolated original-model integrity verifier passes `-B -X utf8` explicitly:
Python's `-I` ignores both `PYTHONDONTWRITEBYTECODE` and `PYTHONUTF8`. Launcher
and descendant evidence records actual UTF-8 and bytecode-write controls, not
just environment strings. Each environment scenario rechecks the complete
payload hashes after execution, including prebuilt stdlib `.pyc` files; cache
changes are failures, never excluded from the inventory.
See the [Python 3.12 command-line contract](https://docs.python.org/3.12/using/cmdline.html).

`accept_release_archive.py` binds these completed reports to the ZIP SHA256,
commit, version, platform and backend. `release_guard.py --validate-assets`
requires exactly the seven approved packages and matching receipts, rejecting
missing, mixed, stale or mislabeled assets. It never interprets a CPU pass as a
GPU result. CUDA and DirectML receipts explicitly say GPU inference was not run.

CUDA packaging selects only `paddlepaddle-gpu`, never co-installs the CPU wheel.
Every vendor DLL/PYD is checked against its wheel RECORD and a packaged hash
inventory. Only the previously byte-pinned Windows NVJitLink static development
archive may be removed (12.6.85: 453,528,212 bytes); its runtime DLL, import
library, notices and metadata remain. Unreviewed NVJitLink versions retain all
files. Other required CUDA/cuDNN/Paddle DLLs and all languages are retained.
Final `.zip.size.json` reports actual complete-package sizes; no runtime-only
measurement is presented as the size of the whole application.

## Server CI handoff

1. Apply the reviewed commits without changing VERSION or existing tags.
2. Run the automatic candidate workflow on the exact pushed commit. Preserve
   every failed/skipped stage and its evidence; do not upload a failed package
   as a release asset.
3. Download `release-candidate-*` artifacts and the per-Server evidence. The
   aggregate job checks the seven profiles and hash-bound receipts.
4. Coordinate any separately authorized version/publication decision only after
   final-package gates pass. Never overwrite 2.3.0 or claim real GPU validation
   from the hosted Server jobs.

## Complete Python source gate

The repository keeps its editable Python GUI, CLI, OCR preprocessing/postprocessing,
model preparation, mode selection and provider routing. Python source execution
uses normal third-party dependencies; it does not require building or supplying
this project's native OCR worker. The native distribution remains a separate,
self-contained delivery format, not a replacement for runnable source.

The final candidate workflow also calls `python-source.yml` for the **same checkout
SHA, repository, run and attempt**. Four independently named source profiles are
required: Linux Python ORT CPU; macOS Python ORT CPU plus CoreML; and Windows
Server 2022 Python Paddle CUDA 11.8 and 12.6, with explicit CPU inference acceptance.
Each starts a new ordinary-pip virtual environment and performs dependency,
model, GUI/CLI, 168-case all-model/three-mode, and three-mode video checks. Mac
runs both providers, including full SRT parity. A compiled CUDA build or CoreML
EP event is not physical GPU certification.

The source workflow records the actual commands, exit codes, interpreter and
clean-environment probe, original output hashes and source identities. Its
aggregate revalidates all four profiles' raw evidence before emitting a separate
`source-acceptance-<sha>-<run>-<attempt>` artifact. The candidate aggregator
requires that workflow to succeed and verifies the externally passed aggregate
hash; it never treats source evidence as final ZIP acceptance, or an older
source commit's successful run as proof for a new commit. These receipts are
unsigned CI execution records, not independent cryptographic attestations.

When called by the candidate workflow, source jobs reuse only that same run's
already verified original Paddle model artifact, then recheck all 45 files.
The manual source workflow prepares its own verified seed. Public source users
use the documented public model-data preparation commands, not private Actions
credentials or these temporary CI artifacts. Diagnostic branch trigger exclusions
are not part of the final production workflow.

### Integrated Qt candidate boundary

The same-run source and Native CoreML jobs consume identical fixed Qt overlay
resource bytes from a dedicated read-only resource preparation job. Independent
manual runs create their own verified resource job; only the parent candidate
caller can reuse an already completed same-run artifact. No skipped/failed
resource producer is silently adopted. Public source preparation does not use
Actions credentials and remains disabled until the fixed new-release resources
and production admission actually exist.

For Mac source, `qt-overlay` is a recorded explicit operation in the newly
created venv after pip-check and before GUI startup. The source receipt binds its
real subprocess, sysconfig query, original preparation receipt, fixed plugin and
native-load evidence. The packaged Mac candidate retains that same plugin,
original pruning inventory, all original wheel RECORDs, licensing, exact small
resource and complete corresponding Qt source. Only the Cocoa pruning entry is
rebound before the full payload/minimum-system/launcher gates run again. The
aggregate additionally extracts and verifies the final Mac ZIP's overlay bytes;
metadata alone or the existing CPU diagnostic cannot satisfy this gate.

These are candidate preparations, not external-AX or full application approval.
All three overlay admission/AX/final-application flags remain false. Published
2.3.0 assets are unchanged, and the proposed 2.3.1 resources are not yet public.
