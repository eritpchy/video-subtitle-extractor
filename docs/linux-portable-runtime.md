# Linux portable native-library closure

The final Linux distribution includes its own CPython and inference dependencies.
Customers do not install Python, pip, Qt, XCB client libraries, or packages with apt.
The operating system still supplies glibc, the ELF loader, a display server for GUI
use, and hardware-specific graphics drivers. This is **not** universal Linux ABI
compatibility. Build on the oldest supported distribution and test the resulting
artifact there. The current Ubuntu 24.04 CI build is not evidence of Ubuntu 22.04
compatibility.

## Build contract

On the trusted Debian/Ubuntu build host, install `binutils` and the application’s
native development/CI prerequisites. Preserve `/var/lib/dpkg` and
`/usr/share/doc/*/copyright`; a stripped container lacking package metadata is not
an eligible release build host. Do not replace missing provenance with guessed
package names.

After portable CPython and curated inference wheels have been installed and
pruned, but before the final artifact inventory is hashed:

```python
from scripts.bundle_linux_runtime_libs import bundle_linux_libraries
native_metadata = bundle_linux_libraries(stage_root, runtime_python=python_path)
```

This scans ELF inputs without running them, recursively resolves `DT_NEEDED`,
copies non-glibc host libraries as regular files into `native-libs/`, and records
source paths, Debian binary/source versions, copyrights and their referenced
common licenses. `native-libs/manifest.json` records per-library SHA-256, sizes,
license hashes, build OS, base libraries, and the **measured** maximum required
numeric GLIBC symbol version across the actual staged and copied ELF files.
Do not use the build host’s glibc version as a substitute for this measurement.

Unknown libraries, unresolved dependencies, escaping symlinks, and missing package
or license metadata fail the build. An interrupted closure is not reusable:
restage a clean bundle. The helper will not overwrite an existing native-libs tree.
Unused optional Qt plugins with unresolved dependencies must be removed by an
explicit reviewed pruning rule, not silently ignored by the closure scanner.

Launchers and build verification subprocesses must set:

```sh
LD_LIBRARY_PATH="$BUNDLE/native-libs"
PYTHONDONTWRITEBYTECODE=1
```

Use the bundled interpreter, never `python` from the customer's PATH. Runtime
`verify_linux_libraries(stage_root)` checks library and attribution hashes without
requiring `readelf`, dpkg, or any external tools on the customer's computer. The
outer release manifest also hashes the complete bundle, including this manifest.

## Required clean-container acceptance gate

The release CI must test the extracted final artifact, not the build venv. Run a
stock Ubuntu image matching the declared supported baseline, **without apt or pip
inside it**, with network disabled. Assert neither `/usr/bin/python3` nor a host Qt
installation exists. Mount only the extracted artifact and writable result/home
directories; do not mount `/usr/lib`, `/opt`, a host venv, or host library caches.
Use the bundled Python path from the package manifest rather than guessing it.

Required checks:

1. Package verification and `scripts/check_runtime.py` using `QT_QPA_PLATFORM=offscreen`.
   This loads Qt, OpenCV, Paddle and lazy CPU math dependencies and renders the
   bundled font. Save the JSON result and process stderr.
2. Native X11 with `QT_QPA_PLATFORM=xcb`. Run Xvfb on the CI **host**, mount only
   its X11 socket and an Xauthority cookie into the container, and forward DISPLAY
   and XAUTHORITY. Do not install Xvfb/Qt into the tested container or disable X11
   access control. Run the existing GUI screenshot/theme tests against the bundle.
3. Actual offline multilingual OCR and ordinary/multiline/AV1/timeline E2E using
   bundled models and fixtures. No network downloads during acceptance.
4. Capture `LD_DEBUG=libs` during GUI and OCR checks. Review loaded paths: every
   non-base client library must resolve inside the bundle. This dynamic check is
   necessary because `DT_NEEDED` alone cannot detect every runtime `dlopen` path.
5. Retain the native manifest, runtime logs, per-artifact bytes, and screenshots as
   CI artifacts. A successful dependency scan is not a substitute for these tests.

For ordinary offscreen checks, the container command has the following shape
(`$BUNDLED_PYTHON_RELATIVE_PATH` is read from the release manifest by CI):

```sh
docker run --rm --network none \
  -v "$EXTRACTED_BUNDLE:/bundle:ro" -v "$RESULTS:/results" \
  -w /bundle ubuntu:24.04 \
  /usr/bin/env -i HOME=/tmp PATH=/usr/bin:/bin \
    LD_LIBRARY_PATH=/bundle/native-libs PYTHONDONTWRITEBYTECODE=1 \
    QT_QPA_PLATFORM=offscreen VSE_OFFLINE=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    "/bundle/$BUNDLED_PYTHON_RELATIVE_PATH" scripts/check_runtime.py
```

This document is a required test contract, **not a claim that the clean-container
checks have passed**. The cloud development executor has no Docker service and
no dpkg package database; its diagnostic dependency scans are not distributable
release builds. The native CI job must establish the missing evidence.

## Executable CI gate

After the final archive has been built, run on the native Ubuntu CI host:

```sh
python scripts/verify_clean_linux_bundle.py \
  --archive dist-ci/vse-2.3.0-linux-x64-cpu.zip \
  --output test-results/clean-linux
```

The output directory must be new or empty. The helper verifies the archive's
SHA-256 sidecar, uses the existing safe ZIP extractor, records the pulled stock
Ubuntu 24.04 image's immutable ID, verifies there is no OS Python/Qt, and runs the
bundled runtime check and full bootstrap GUI/ordinary/issue E2E acceptance. It
then launches host `xvfb-run` with authentication intact and mounts only its Unix
socket/cookie for container XCB GUI screenshots (`QT_X11_NO_MITSHM=1` avoids
cross-IPC MIT-SHM). The container runs as the host UID, with a read-only bundle
and filesystem, writable external evidence/home/cache, and no network. Loader
traces cover Python children and dynamically linked VSF/FFmpeg subprocesses.

The gate rejects missing Docker/Xvfb/xauth, missing workload evidence, incomplete
screenshots, and any actually initialized non-base shared library outside the
bundle. Neither a skipped command nor a missing loader trace counts as passing.
Retain the entire output directory on CI success **or failure**. A local unit-test
pass only proves command construction/log validation, not this container gate.

The clean container deliberately retains `PATH=/usr/bin:/bin` **after** its
no-Python/no-Qt baseline assertion. The verified VSF 6.10.6 shell wrapper requires
the base OS `dirname` utility; shell/coreutils are OS facilities, not inference
dependencies. The bundled interpreter is always invoked by its explicit absolute
path, and external Python/Qt/non-base libraries remain prohibited. Do not patch
the checksum-verified VSF wrapper or install missing tools inside acceptance.
