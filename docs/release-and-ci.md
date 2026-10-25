# CPU release, preview and CI

## What is supported by these workflows

- Python 3.12, CPU: Linux x64 (`ubuntu-24.04`), Windows x64 (`windows-2022`), macOS Apple Silicon (`macos-15`).
- Docker: headless Linux amd64 CPU image. It is built and exercised in CI, not automatically pushed to a registry.
- Native jobs install dependencies, run `pip check`, verify VideoSubFinder 6.10, download checksum-pinned PP-OCRv6 small/medium models, run unit/real OCR/real VSF tests, launch the actual GUI offscreen in light/dark modes and with each native Qt platform backend (Linux X11 via Xvfb, Windows/macOS default), and run video-to-SRT extraction.
- Package jobs include both verified PP-OCRv6 small and medium tiers, the native VSF binary and its libraries, the application source, and native offline Python wheels. Each archive is extracted, checksum-checked, installed into a fresh virtual environment without a package index, and tested with the GUI, CLI and real extraction.

**A workflow definition is not a test result.** Read the completed run for the exact commit before claiming a platform passed. Offscreen rendering and hosted-runner native GUI checks do not prove display-driver/window-manager behavior on every desktop. CUDA, DirectML, Intel macOS, Windows ARM, Linux ARM and GPU Docker are not certified by this CPU release. The old QPT/CUDA/DirectML workflows were removed: they ran on obsolete Windows runners and could publish or mutate the same prerelease on every branch push.

CI runs on pushes and pull requests with read-only repository permissions. Test reports, extraction logs and generated SRTs are uploaded as Actions artifacts. Workflow failures must be fixed and rerun on the final commit; skipped, cancelled and still-running jobs are not passes.

## Safe preview

1. Commit the intended changes and push the review branch. Wait for **Cross-platform CI** to pass for that commit.
2. Open **CPU release preview and publish**, choose that branch, leave `publication=artifacts`, and run it.
3. Review all CI jobs and three `cpu-bundle-*` artifacts. Artifact-only preview creates no tag and no GitHub release.
4. Download the appropriate ZIP, verify its `.sha256` file, extract it into a writable directory, and run its launcher. Review the embedded `bundle-manifest.json` for the exact source commit and file hashes.

These are **CPU source + offline-wheelhouse bundles, not standalone EXE/app installers**. Install a native Python 3.12 from [python.org](https://www.python.org/downloads/) first. On Windows include the `py` launcher; on macOS use native arm64 Python, not an Intel Python under Rosetta. Then:

- Windows: double-click `Start VSE.cmd` or run `py -3.12 bootstrap.py`.
- macOS: run `python3.12 bootstrap.py` (or `Start VSE.command` after reviewing the downloaded source).
- Linux: install the system Qt/OpenGL libraries below, then run `./start-vse.sh`.
- Command-line extraction: `python3.12 bootstrap.py --cli /path/video.mp4 --cpu --output /path/video.srt` (use `py -3.12` on Windows).
- Recheck the extracted bundle: `python3.12 bootstrap.py --verify`.

The first launch verifies the payload and creates a local `.venv` from the included wheelhouse. No PyPI access is needed. PP-OCRv6 small/medium models are included; other language/model combinations retain their documented compatibility limitations. `--verify` detects changes to packaged files; normal subsequent launches reuse the already installed virtual environment. ZIP checksums detect corruption, not publisher identity: download from the intended repository's release or authenticated Actions run.

Linux runtime libraries (Ubuntu/Debian):

```sh
sudo apt-get update
sudo apt-get install libgl1 libegl1 libglib2.0-0 libdbus-1-3 libxkbcommon0
```

A real X11 desktop may also need its normal Qt xcb runtime libraries. Offscreen CI uses `QT_QPA_PLATFORM=offscreen`; do not force that variable for a visible desktop app.

## Publication and immutable tags

Publication is manual and separate from preview. Select `draft`, `prerelease`, or `release` and supply a tag matching `backend/config.py`'s VERSION, optionally with a new preview suffix (for example `2.2.0-preview.1`). The workflow repeats CI and packaging, then publishes only after every required job passes. Configure required reviewers on the GitHub **release** environment if a second-person gate is desired.

The guard verifies the dispatch checkout's exact SHA and refuses:

- a tag that already points at a different commit;
- an existing release, including drafts/prereleases, rather than replacing its assets;
- a version/tag mismatch, failed API lookup, missing native bundle or failed checksum.

**The existing `2.2.0` tag is immutable.** If it points at an older commit, do not move it to publish this work. Use a new preview tag for the current application version or bump the version and use a new release tag. Promoting an existing prerelease is a separate reviewed operation; this workflow intentionally cannot overwrite/promote it. A GitHub release's `target_commitish` display is not proof of the tag's actual commit: check the peeled tag SHA.

The final creation uses a non-force tag push and `gh release create --verify-tag`. A failure after tag creation may leave a tag without a release; inspect that exact tag before retrying. A successful prior release makes a rerun fail closed instead of overwriting anything. Existing 2.2.0 assets and historical tags are never deleted by these workflows.

## Local validation

```sh
python -m pip install -r requirements.txt pytest==8.3.5
python -m pip check
python scripts/install_videosubfinder.py --version 6.10
python -m backend.tools.model_download --tiers small medium
QT_QPA_PLATFORM=offscreen VSE_RUN_OCR_INTEGRATION=1 VSE_RUN_VSF_INTEGRATION=1 python -m pytest tests -q
QT_QPA_PLATFORM=offscreen python gui.py --smoke-test
python scripts/e2e_smoke.py --output test-results/e2e
```

Set the same environment variables with PowerShell `$env:NAME='value'` on Windows. To create a release archive, use a clean, committed checkout on the target OS/architecture:

```sh
python -m pip wheel -r requirements.txt pytest==8.3.5 --wheel-dir build/wheelhouse
python scripts/package_release.py --wheelhouse build/wheelhouse --output dist
python scripts/package_release.py --verify dist --smoke
```

Use a clean wheelhouse for each build; duplicate versions are rejected. Do not copy a Windows wheelhouse into a macOS or Linux bundle. CPU bundles contain third-party dependencies and their packaged license metadata; the application remains Apache-2.0 licensed. The old `backend/tools/makedist.py` QPT helper is legacy and not the release path.

Runner labels are explicit rather than `*-latest`: see [GitHub's official runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners). A manually dispatched workflow must first be available on the repository's default branch; see [GitHub's workflow-dispatch documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch).
