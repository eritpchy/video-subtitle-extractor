# VideoSubFinder CLI integration

VSE 2.3.0 uses the official `eritpchy/videosubfinder-cli` **v6.10** release on
Linux, Windows and macOS. The CLI's version was aligned with upstream, so older
fork tags with a higher-looking version must not be selected by sorting tags.
The old bundled Windows GUI executable is not used as a CLI fallback.

## Install

```sh
python scripts/install_videosubfinder.py
python scripts/install_videosubfinder.py --verify-only
```

The installer selects the native x64/arm64 release, downloads `SHA256SUMS` from
the same official release, checks the archive, rejects unsafe archive paths and
links, and validates the native binary and settings before replacing an existing
installation. It does not use the GitHub API or require a GitHub token.

Installed files remain together in `backend/subfinder/<platform>/<arch>/`.
Linux and Windows CPU releases are static; macOS includes its `lib` directory.
No system OpenCV or FFmpeg is needed by these binaries. The application still
requires its normal Python dependencies. Native macOS execution is tested by
upstream on macOS 15; older versions are not claimed as verified.

The installer preserves existing VSE `settings/general.cfg` tuning, adding new
release defaults. Legacy keys and Windows `text_alignment = Center` are migrated
to the current format. Inherited `use_cuda_gpu` is disabled so GPU activation
always goes through the CLI's explicit capability check.

Offline install with an official archive and its matching manifest:

```sh
python scripts/install_videosubfinder.py --archive videosubfinder-cli-linux-x64.tar.gz --checksums SHA256SUMS
```

For cross-platform packaging use `--platform linux|windows|macos` and
`--arch x64|arm64` (`universal` is also available for macOS). `--destination DIR`
overrides the installation directory. A cross-platform download is integrity
checked but is not advertised as execution-tested until run on its native host.

## Runtime overrides

- `VSE_VSF_BINARY`: explicit path to a modern CLI executable or `.run` launcher.
- `VSE_VSF_DIR`: directory containing a modern portable CLI bundle.
- `VSE_VSF_CUDA=1`: explicitly request CUDA. First install the separate Linux x64
  CUDA 12 package with `python scripts/install_videosubfinder.py --cuda`. That
  package requires a compatible NVIDIA driver and glibc 2.35+. OCR acceleration
  does not automatically enable CLI CUDA.

## Completion and failure handling

Arguments are passed as an argv list without a shell, including paths with
spaces or Unicode. Search threads are clamped to the supported range 2–64.
Both output streams are drained so a full pipe cannot deadlock the child.
`Progress: 100%` is held at 99% in the application: success requires EOF, `FINISH`
and exit code zero. Decoder/startup errors propagate instead of producing a
false successful extraction. Frame timestamps include zero, are deduplicated and
sorted, and convert to frame indices using milliseconds × FPS / 1000.

## Tests

```sh
python -m pytest tests/test_videosubfinder.py tests/test_extractor_lifecycle.py -q
VSE_RUN_VSF_INTEGRATION=1 python -m pytest tests/test_videosubfinder.py -q
```

The second command performs real generated-video extraction with the installed
binary and checks subtitle intervals, Unicode paths, and timing-only output.
The environment-variable syntax above is POSIX; set it with `$env:...` in
PowerShell. CI sets it automatically on Linux, Windows and macOS.
