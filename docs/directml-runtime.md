# Restored DirectML runtime

`VSE_OCR_BACKEND=directml` routes both full subtitle OCR and standalone detection
through `backend/tools/directml_ocr.py`. The package uses the original
`onnxruntime-directml==1.20.1` wheel, including its DirectML **1.15.2** DLL.
It does not replace this with the experimental native-worker SDK, ORT 1.24.4,
DirectML 1.15.4, or a client-side model converter.

## Offline assets and packaging

`python scripts/package_release.py --profile directml --onnx-models PATH ...`
consumes the exact `native/ocr/prepare_models.py` output:

- All 15 hash-pinned FP32 ONNX graphs, with unchanged IR 8 / opset 17.
- The 13 matching model/config JSON files and both provenance manifests.
- The original DirectML 1.15.2 license and third-party notices, separately from
  ONNX Runtime's MIT notices. `docs/licenses/directml-1.15.2/source.json` records
  original upstream URLs and byte hashes; it does not introduce a new runtime.

The bundle contains `directml-models/`; its isolated bootstrap fixes
`VSE_DIRECTML_MODEL_ROOT` to that package-local directory. Staging verifies the
repository's source-pins document hash, then every model/config hash, and rechecks
copied bytes. No Paddle, PaddleX, PaddleOCR, Paddle2ONNX or separate CPU ORT wheel
is allowed in the DirectML runtime. The packaging audit also checks the actual
DirectML DLL hash, not just a distribution name/version.

## Honest provider selection

Default inference requests `DmlExecutionProvider`. It disables memory-pattern
optimization, uses sequential execution, and serializes calls to each engine.
The Python ORT wrapper can internally fall back during session creation, so the
adapter verifies the resulting session's providers and disables further fallback.
On each model's first real inference, ORT profiling must show an executed
DirectML kernel. A registered provider with an entirely CPU graph is rejected.
CPU helper/shape-node counts, when present, remain visible in execution evidence.
Failures and device loss are not retried on CPU.

The existing user `--cpu`/disable-acceleration control explicitly selects
`CPUExecutionProvider`; it is labeled CPU and is never GPU verification. This
makes GPU-less Windows Server CI useful without pretending it tests hardware.
Missing DML hardware with the default GPU selection produces an actionable error.

## Reproducible verification

```sh
# Source checkout + already installed original ORT 1.20.1 and runtime dependencies
python scripts/check_directml.py --models PATH --provider cpu --report cpu.json
python scripts/check_directml.py --models PATH --provider directml --report dml.json
```

Both commands validate all assets and compare the adapter against the existing
110 frozen image/detection/recognition goldens at the original confidence
threshold, never regenerate expected results. The verifier retains at most one
model pair at a time so the 15-model check does not accumulate all ORT arenas.
The CPU command is an explicit CPU parity gate, not a hardware claim. The DML
command additionally requires actual DirectML kernel execution; if run on a
software adapter such as WARP, the result is still not GPU-performance evidence.

As of this restoration, all 15 original graphs have loaded and executed under
ORT 1.20.1 CPU with finite outputs. The complete adapter also passes **110/110**
frozen CPU parity cases: boxes/text identical, maximum confidence difference
`2.384185791015625e-7` against the unchanged `1e-5` threshold. Actual Windows DML execution, GPU accuracy /
performance and the final no-network portable Windows package remain independent
Server-CI gates; no test on the user's computer is required or authorized.

Source-level integration evidence also includes real Qt/Pillow/font and Chinese
OCR checks; all `fast`/`accurate`/`auto` video-to-SRT modes using verified
VideoSubFinder 6.10.7; and multiline/AV1/timeline regressions. These ran with
explicit CPU selection, separately from default-DML GUI-only launch (which does
not initialize inference sessions). The Windows evidence recorder accepts the
DirectML profile but continues to label its Server acceptance as CPU-only and
does not inherit the experimental native-worker SDK license gate.
