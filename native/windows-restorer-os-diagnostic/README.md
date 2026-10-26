# Accepted-306 Windows 2025 / CUDA 12.6 OS diagnostic

This is a separate **Server-only diagnostic**, not a customer release, a GPU
test, or a replacement for the accepted-package gates. The original four-job
run `40308041109`, at `7f450e98c0991ec66f29578f732d5c4d322eef3b`, had three
successful jobs and one failed Windows Server 2025 / `paddle-cuda12.6` job.
That job completed native restoration and full original-payload SHA checks;
its clean-host launcher scenario failed when the original PowerShell OS
collection exceeded its unchanged 60-second timeout. Its contaminated-host
scenario passed. The first failure and its raw evidence remain failures;
a later successful run or probe cannot overwrite or upgrade them.

The dedicated `windows-restorer-os-diagnostic.yml` runs only Windows Server
2025 and the fixed accepted-306 CUDA 12.6 input. Push triggers require the exact
`fix/windows-restorer` branch and this new sibling directory or the new workflow
path; manual dispatch is retained. Neither the old four-job accepted workflow
nor the synthetic restoration/rename-probe workflow is changed or requested.
The original wrapper, accepted pins and native restoration implementation stay
unchanged. The exact baseline source checkout remains
`3064320ec74e1aa71f3702c2ea966485f4b31e33`.

## Order and failure semantics

1. Run the existing accepted-baseline contract tests and the independent
   diagnostic tests. These offline tests are not native Server evidence.
2. Invoke the unchanged `run_windows_restorer_accepted_ci.py` for the complete
   original acquisition, full-SHA native restoration, independent tree audits,
   and both original-launcher scenarios, including the real three-mode explicit
   CPU and 15-model gates. No added OS diagnostic runs before this acceptance.
3. The separate `post_probe.py` runs afterward even if acceptance failed. It
   first preserves the original outcome and report hashes, then writes only
   new diagnostic evidence outside the restored payload and original report
   directories. Missing original evidence is a failure, not permission to
   manufacture a substitute acceptance result.
4. Probe through the actual verified bundled Python using standard-library
   facilities. Extract the fixed PowerShell command and 60-second timeout from
   the SHA-verified `windows_verification.py` AST without executing arbitrary
   extracted Python. Compare the original `SystemRoot` executable route with
   the absolute route obtained using `GetSystemDirectoryW`. Keep one original
   command control and one stderr-stage-marker variant for each probed
   configuration, with no retry or enlarged timeout. A marker variant does
   not replace the original control or original acceptance command.
5. Collect `sys.getwindowsversion`, `RtlGetVersion`, `IsWow64Process2` and
   read-only registry observations as separate comparisons, never replacements
   for the original CIM identity gate. Recheck the complete restored tree,
   original ZIP and sidecars, all byte volumes, and frozen report hashes after
   probing, including failure paths.

No step uses `continue-on-error`. An original acceptance failure, probe failure,
missing evidence, or integrity mismatch must retain a nonzero overall result.
The original gate and probe outcomes remain distinct in the evidence. A failed
probe must not prevent collection of other available diagnostic observations
or the final integrity checks.

## What the probe can establish

The probe derives a **new same-configuration environment** from the baseline-306
fixture constructor, original sanitizer and the current run's actual
`environment-report.json`. That report is intentionally selective, not a full
environment dump. Record provenance and missing fields; do not claim byte-for-
byte identity with the old failure instant, or with an unrecorded complete
environment. Retain only necessary safe values, non-sensitive key names and
filtering booleans. Never dump tokens or the entire inherited environment.
Do not write fresh probe/audit records into the frozen original evidence.

“No added prewarming” does **not** mean a cold WMI/CIM service: the unchanged
wrapper already calls `platform.platform()`, and CPython 3.12 can query WMI
there. Moreover, a later control, marker variant, or contaminated-host scenario
runs at a different time and may see warmed system state. Success after the
first failure cannot establish its cause. Missing early markers likewise do
not prove that `Get-CimInstance` was reached. Preserve partial output, timing,
errors and source/executable identities rather than infer an unobserved stage.

The experiment does not modify the original package, registry, system locale,
security policy, services or timeout policy. It neither installs client
dependencies nor repairs or restarts WMI. Acquisition is the existing pinned
artifact download; diagnostic queries are local. No GPU execution, Windows
10/11 client compatibility, or final-release acceptance is implied.

The complete original acceptance evidence is retained, including the original
GUI screenshots and generated test media, alongside reports/logs, checksum
sidecars and reviewed source companions. New probe `stdout.bin` and `stderr.bin`
are raw captured output streams, not executables: their exact bytes are retained
even if invalid UTF-8. Only those two exact `.bin` basenames are added to the
diagnostic upload allowlist. No customer EXE, object/library, original ZIP or
byte volume is uploaded or published. New diagnostic results require the actual
Server run; this document does not claim that those probes have already
executed or passed.
