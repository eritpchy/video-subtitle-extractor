# CPU release, preview and CI

## What is supported by these workflows

- Python 3.12, CPU verification: Linux x64 (`ubuntu-24.04`), Windows **Server 2022 and Server 2025 x64** (`windows-2022`, `windows-2025`), macOS Apple Silicon (`macos-15`). Windows verification is deliberately Server CI only: Windows 10/11 x64 clients, Windows ARM/emulation and physical GPU execution are outside the selected coverage, not gates awaiting machine authorization; see the [Windows verification matrix](windows-verification.md).
- Docker: headless Linux amd64 CPU image. It is built and exercised in CI, not automatically pushed to a registry.
- Native jobs install dependencies, run `pip check`, verify VideoSubFinder 6.10.7, download checksum-pinned PP-OCRv6 small/medium models, run unit/real OCR/real VSF tests, launch the actual GUI in light/dark modes using Cocoa on macOS and offscreen on Linux/Windows, then exercise each native Qt platform backend (Linux X11 via Xvfb, Windows, Cocoa), and run video-to-SRT extraction.
- Package jobs include both verified PP-OCRv6 small and medium tiers, the native VSF binary and its libraries, the application source, a checksum-pinned portable Python interpreter and the preinstalled inference runtime. Each archive is extracted, checksum-checked and tested using its own interpreter with host Python removed from PATH. No client installation occurs.
- Both CPU workflows additionally require final-ZIP actual-launcher acceptance with clean PATH and simulated host environment contamination on every native platform: Windows, Linux XCB under Xvfb, and macOS Cocoa `.command` plus audited `.app` CLI. These gates run only after earlier checks succeed; their reports stay in separate OS/profile artifacts. See [portable environment isolation](portable-environment-isolation.md).

**A workflow definition is not a test result.** Read the completed run for the exact commit before claiming a platform passed. Offscreen rendering and hosted-runner native GUI checks do not prove display-driver/window-manager behavior on every desktop. CUDA, DirectML, Intel macOS, Windows ARM, Linux ARM and GPU Docker are not certified by this CPU release. The old QPT/CUDA/DirectML workflows were removed: they ran on obsolete Windows runners and could publish or mutate the same prerelease on every branch push.

CI runs on pushes and pull requests with read-only repository permissions. Test reports, extraction logs and generated SRTs are uploaded as Actions artifacts. Workflow failures must be fixed and rerun on the final commit; skipped, cancelled and still-running jobs are not passes.

## Safe preview

For the first manual run, GitHub requires this workflow file to exist on the
repository's default branch. Integrate the reviewed workflow there before
dispatching the exact tested release branch; do not add automatic publication
triggers as a workaround.

1. Commit the intended changes and push the review branch. Wait for **Cross-platform CI** to pass for that commit.
2. Open **CPU release preview and publish**, choose that branch, leave `publication=artifacts`, and run it.
3. Review all CI jobs and three `cpu-bundle-*` artifacts. Artifact-only preview creates no tag and no GitHub release.
4. Download the appropriate ZIP, verify its `.sha256` file, extract it into a writable directory, and run its launcher. Review the embedded `bundle-manifest.json` for the exact source commit and file hashes.

These are **self-contained CPU one-click bundles**. Customers do not need to
install Python, pip, packages or models for the bundled V6 small/medium paths.
Keep the extracted folder together and use its launcher:

- Windows: double-click `Start-VSE.bat`.
- macOS: open `Video Subtitle Extractor.app`.
- Linux: run `./Start-VSE.sh` on a compatible graphical desktop.
- CLI/verification: pass `--cli ...` or `--verify` to the included launcher.

The interpreter and inference libraries are already installed during the build.
There is no wheelhouse, client `.venv` creation, first-run package installation or
hidden runtime download. Existing legacy-language model limitations remain
explicit in [model coverage](PP-OCRv6.md). The original ZIP is the offline repair
source. ZIP checksums detect corruption, not publisher identity; use the intended
repository release or authenticated Actions artifacts.

Linux bundles must contain the non-base GUI/native dependency closure and pass
native X11 verification; a build-runner `apt install` is not evidence that an
unprepared customer machine will work. Native OS/driver compatibility is bounded
by the tested release matrix. macOS verification uses a real Cocoa login session,
not Qt offscreen handles. No platform is claimed until its final bundled runtime
passes the native CI gate.

## Publication and immutable tags

Publication is manual and separate from preview. Select `draft`, `prerelease`, or `release` and supply a tag matching `backend/config.py`'s VERSION, optionally with a new preview suffix (for example `2.3.0-preview.1`). The workflow repeats CI and packaging, then publishes only after every required job passes. Configure required reviewers on the GitHub **release** environment if a second-person gate is desired.

The guard verifies the dispatch checkout's exact SHA and refuses:

- a tag that already points at a different commit;
- an existing release, including drafts/prereleases, rather than replacing its assets;
- a version/tag mismatch, failed API lookup, missing native bundle or failed checksum.

**This feature release targets the new `2.3.0` version.** The existing `2.2.0` tag, release status and assets are out of scope and must remain untouched. New feature work uses a minor-version increment; fixes within a published feature version use a patch increment. No repository-specific version policy beyond semantic numeric VERSION/tags was present. This workflow cannot overwrite or promote an existing release. A GitHub release's `target_commitish` display is not proof of the tag's actual commit: check the peeled tag SHA.

The final creation uses a non-force tag push and `gh release create --verify-tag`. A failure after tag creation may leave a tag without a release; inspect that exact tag before retrying. A successful prior release makes a rerun fail closed instead of overwriting anything. Existing 2.2.0 assets and historical tags are never deleted by these workflows.

## Local validation

```sh
python -m pip install -r requirements.txt pytest==8.3.5
python -m pip check
python scripts/install_videosubfinder.py --version 6.10.7
python -m backend.tools.model_download --tiers small medium
QT_QPA_PLATFORM=offscreen VSE_RUN_OCR_INTEGRATION=1 VSE_RUN_VSF_INTEGRATION=1 python -m pytest tests -q
QT_QPA_PLATFORM=offscreen python gui.py --smoke-test
python scripts/e2e_smoke.py --output test-results/e2e
```

On macOS, replace `QT_QPA_PLATFORM=offscreen` with `QT_QPA_PLATFORM=cocoa` for the pytest and GUI commands above, and run from a graphical login session. The release bootstrap selects Cocoa automatically on macOS.

Set the same environment variables with PowerShell `$env:NAME='value'` on Windows. To create a release archive, use a clean, committed checkout on the target OS/architecture:

```sh
python -m pip wheel -r requirements.txt -r requirements-inference.txt pytest==8.3.5 --wheel-dir build/wheelhouse
python scripts/package_release.py --wheelhouse build/wheelhouse --output dist
python scripts/package_release.py --verify dist --smoke
```

Use a clean wheelhouse for each build; duplicate versions are rejected. Do not copy a Windows wheelhouse into a macOS or Linux bundle. CPU bundles contain third-party dependencies and their packaged license metadata; the application remains Apache-2.0 licensed. The old `backend/tools/makedist.py` QPT helper is legacy and not the release path.

Runner labels are explicit rather than `*-latest`: see [GitHub's official runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). A manually dispatched workflow must first be available on the repository's default branch; see [GitHub's workflow-dispatch documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).


### Inference-only runtime and size accounting

`requirements.txt` remains the development environment. The builder resolves
`requirements-inference.txt` against native official wheels, installs them into
the pinned portable interpreter, then excludes every wheel/cache from the final
archive. Official wheels are not repacked. The shipped derived installation
retains licenses and original `RECORD` metadata, with an explicit per-file
`runtime/inference-prune.json` explaining removals and hashing retained files.
Reviewed test assets, developer headers/type metadata and generated bytecode are
omitted; supported launchers disable bytecode regeneration. Only the tested Linux
x64 Paddle 3.3.1 `engine=paddle` profile omits its unused OpenVINO CPU plugin;
hard-linked OpenVINO core, MKL, oneDNN and Paddle inference libraries remain.

Two exact metadata edges deliberately substitute narrower official providers:

- Fluent Widgets → PySide6 uses matching Essentials/Shiboken instead of the broad
  Addons/WebEngine/PDF meta-package.
- macOS Frameless Window → PyObjC uses same-version Core/Cocoa/Quartz, with actual
  native imports checked, instead of all macOS SDK wrappers.


All other active requirements, OCR extras and provider version constraints must
pass the strict runtime audit. This is not a claim that ordinary `pip check`
accepts the intentionally narrower metadata substitutions. Native GUI/theme,
multilingual OCR, AV1, multiline subtitles and timeline tests gate the profile.
All existing bundled language-model coverage is retained; only model directories
unreachable from production selection and non-runtime marketing media are removed.

`*.zip.size.json` records compressed download and complete extracted bytes.
`test-results/runtime-size.json` records the actual included runtime, models and
total footprint per native platform. Download size is not installed size; neither
requires an additional package-installation expansion on the client. CI also
preserves the full pruning audit manifest. Docker uses the same audited inference
profile in a multi-stage build and retains no builder wheel/cache layers.
