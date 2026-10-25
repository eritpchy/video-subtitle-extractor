# Headless CPU Docker

The Dockerfile targets **Linux amd64 / CPU**. It packages Python 3.12, pinned project dependencies, a checksum-verified VideoSubFinder 6.10 CLI, and genuine PP-OCRv6 small and medium models. The default entrypoint is the noninteractive subtitle CLI, not the desktop GUI. No image registry is modified by CI.

## Build and inspect

```sh
docker build --platform linux/amd64 -t vse:2.3.0-cpu .
docker run --rm vse:2.3.0-cpu --version
docker run --rm vse:2.3.0-cpu --help
```

The build needs internet access to PyPI, the official VSF GitHub release, and official Paddle model URLs. Downloads are checksum-verified where an upstream checksum manifest is supplied. The base image and resolved transitive Python dependencies can change between builds; release ZIPs record their exact native wheels and checksums. An image built locally is not a signed/reproducible release artifact.

## Extract subtitles

Mount a writable directory at `/data` and specify a subtitle region as relative coordinates `ymin,ymax,xmin,xmax`:

```sh
docker run --rm \
  -v "$PWD/media:/data" \
  vse:2.3.0-cpu \
  '/data/input video.mp4' --output /data/output.srt \
  --language en --mode fast --cpu --area 0.6,1,0,1
```

The container runs as UID 1000, not root. The mounted output directory must be writable by UID 1000, or choose an appropriate local directory/ownership before running. Input and output paths may contain spaces; quote them. Existing output is not replaced unless `--overwrite` is explicitly supplied. Do not mount credentials or unrelated home directories. Container-internal temporary frames are removed with the container; finished SRT output persists in the mounted directory.

Small/medium PP-OCRv6 models and the VSF runtime are embedded during the build, so basic CPU extraction can run without network access:

```sh
docker run --rm --network none -e VSE_OFFLINE=1 -v "$PWD/media:/data" vse:2.3.0-cpu \
  /data/input.mp4 --output /data/output.srt --cpu --area 0.6,1,0,1
```

Some non-v6 language/model paths may have additional model requirements. No GPU passthrough, GUI forwarding, macOS-native or ARM image is claimed here; Docker Desktop on ARM may emulate amd64 slowly.

## Real container regression

```sh
docker run --name vse-smoke --network none -e VSE_OFFLINE=1 --entrypoint python vse:2.3.0-cpu \
  scripts/e2e_smoke.py --output /tmp/vse-e2e
docker cp vse-smoke:/tmp/vse-e2e ./test-results/docker-e2e
docker rm vse-smoke
```

This generates a synthetic subtitle video, invokes real VSF and PP-OCRv6, and checks recognized SRT text and timestamps. Read `report.json`, `recognized.srt`, and `extraction.log`. A successful `--help` alone is not an end-to-end test. The CI Docker job runs the same extraction and uploads its evidence; do not claim Docker passed until that job completes successfully for the intended commit.


## Inference runtime size

A multi-stage build resolves the same audited inference-only wheel closure as
native bundles, then copies only `/opt/venv` into the final image. Build-only
wheels, pip caches, Qt WebEngine/Addons/PDF, SciPy/scikit-image, LMDB and generated
Python bytecode are not retained in runtime layers. The explicit Fluent Widgets
PySide6 meta-package substitution is described in [release-and-ci.md](release-and-ci.md).
The non-root runtime rechecks dependency compatibility and real native imports.
All selected V6 tiers and reachable bundled language fallback models are retained.
Measure final image size separately from builder cache size; build caches are not
part of the shipped runtime image.
