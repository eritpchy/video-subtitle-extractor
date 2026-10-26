# Fixed accepted-306 real CUDA package experiment

The `29f9cee` candidate completed **19/19** native cases on both Server versions
in run `40299168461` (jobs `121492532768` / `121492533219`). The space case
continues to simulate an insufficient Win32 API report; it did not exhaust a
physical disk. No real CUDA package or GPU inference result follows from those
synthetic cases alone.

A separate `windows-restorer-accepted.yml` now prepares four jobs only on the
exact `fix/windows-restorer` branch and dedicated experiment-file push paths
(with manual dispatch retained for registered workflows):
two original accepted CUDA packages, each on Server 2022 and Server 2025. It
never changes the original synthetic/probe gates. The committed
`accepted-baseline-306.json` identifies source
`3064320ec74e1aa71f3702c2ea966485f4b31e33`, run `40288916991`, exact artifact IDs,
whole artifact ZIP SHA256/bytes, original inner ZIP SHA256/bytes, checksum SHA,
and observed expanded sizes. No artifact is selected by a mutable name or latest
run. These are **306 baseline** inputs, not the older 597 layout and not a future
release. Different final source/ZIP hashes require a fresh complete real-package
gate. An expired/missing artifact or metadata/hash mismatch fails closed.

Acquisition uses read-only GitHub Actions access and verifies the outer container
before unwrapping exactly the original ZIP and its two original sidecars. The
large payload ZIP is not extracted by Python. The original final
`.zip.acceptance.json` must exist inside that fixed container and pass the shared
release guard. A `package-verification.json` is **not** substituted for that final
receipt. Original receipt bytes are retained unchanged; a new diagnostic report
never overwrites or upgrades the old acceptance or invents GPU verification.

For each job, let `Z` be the inner ZIP size, `A` the artifact-container size, `E`
the actual expanded bytes, and `N` the verified native inventory count including
implicit directories. The native reserve is `M = N * 65,536 + 64 MiB`. Keeping
the original ZIP, all parts, the native reconstructed ZIP and the output at the
same time requires `3Z + E + M`. The diagnostic adds a 4-GiB allowance for
build/source/launcher/video evidence. Initial download checks `A + Z + 4 GiB`;
after verification only its own outer transfer container is removed. Then the
remaining-free-space check requires `2Z + E + M + 4 GiB`, because the retained
original `Z` is already allocated. No system/user files are cleaned up and no
space-setting or security-policy change is attempted. Every measurement is real
and recorded; preflight does not reserve space against other processes.

| Baseline | Original ZIP bytes | Expanded bytes | Peak including 4-GiB reserve, before `N * 65,536` |
| --- | ---: | ---: | ---: |
| CUDA 11.8 | 2,985,646,447 | 5,316,516,832 | 18,635,532,333 bytes (17.356 GiB) |
| CUDA 12.6 | 2,696,632,894 | 4,498,964,750 | 16,950,939,592 bytes (15.787 GiB) |

`N` is computed from the actual downloaded 306 archive, not borrowed from an
older package. Part bytes remain 2,000,000,000; part hashes, original full SHA,
fixed index/header, compiler/source/object/map/PE records and actual EXE size are
recorded. The public production builder is used with **no synthetic space shim**.
The original native code enforces the reconstructed whole-ZIP SHA before
fixed-range extraction. An independent Python traversal then checks every file's
size/SHA and rejects extra entries or reparse points, both before and after the
launcher workloads. Python never substitutes for native extraction.

The restored package's **original `Start-VSE.bat`** is invoked through the existing
`launcher_command` helper, with baseline-306 audit helpers and frozen model
fixtures from a separate exact-commit checkout. Both clean PATH and deliberately
contaminated child environments must pass real GUI/media/three-explicit-CPU-mode,
15-model and loaded-runtime isolation validation. No CI download token is
forwarded to application children. There is no alternate executable/interpreter
launcher or re-extraction of the input ZIP to manufacture a passing app root.
The real-package destination includes Unicode, spaces and `&!()`; `%` remains
covered by native restoration's independent synthetic gate, but baseline's batch
launcher helper rejects `%` arguments. These different support boundaries must
not be conflated or bypassed by silently moving the payload to an ASCII path.

Only logs, hashes, exact restorer source/header and verification evidence are
uploaded. No customer EXE, payload, volume, tag or release asset is published.
The four-job experiment is not yet reported as executed or passed here; actual
Server reports are required. It establishes neither Windows 10/11 customer
certification, genuine disk exhaustion, nor CUDA GPU inference.

The original synthetic workflow, its paths and `run_windows_restorer_ci.py` stay
unchanged. The separate wrapper reuses its reviewed `load_toolchain` and
`select_installed` functions instead of inventing a second compiler selector.
Its extra orchestration sources accompany the original exact compile closure
as separate diagnostic evidence; neither is a distributed customer binary.
