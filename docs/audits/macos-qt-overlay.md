# Explicit macOS Qt AX overlay: preparation, not release approval

The exact Qt 6.12.0 Cocoa plugin built from QtBase plus upstream Gerrit 772484
patch set 6 is prepared for the eventual **new VSE 2.3.1 release**. Nothing here
publishes that release, edits an existing asset/tag, changes repository settings,
disables accessibility, downgrades Qt, or certifies the integrated application.
`native/qt/macos-qt-overlay.json` currently keeps production admission **blocked**.
Historical `candidate.json` bytes and its diagnostic/production-false fields are
never rewritten to manufacture approval.

## Exact public resources (not published by this change)

The eventual release's fixed asset URLs are in the policy. No `latest` URLs,
private Library identity, expiring Actions artifact or ambient token is used by
the public preparation path. Until publication and admission, use explicit
local bytes for authorized candidate tests; the public download path is blocked.

| Asset | Bytes | SHA256 |
| --- | ---: | --- |
| `vse-qt6.12.0-macos-arm64-ax772484-ps6.zip` | 2547078 | `9ffa26a3ecf5296d9d9eb9841789307e75aa352eafc9539f31ae80e382135441` |
| `vse-qtbase6.12.0-ax772484-ps6-source.tar.xz` | 51103812 | `5c4df52a68232eac6fb40cc4f09ed93ff92d30c8ab8bf88f245cee6b482238bd` |

The small ZIP includes the original signed plugin bytes, all Qt license texts,
modified Cocoa source and Apple attribution, the exact PS6 patch, historical
candidate/build pins, and rebuilding instructions. The separate source archive
contains all QtBase files with exactly the PS6 three-file patch applied (two
production sources plus its upstream regression test). It is not just a URL or
a partial source excerpt. The full source and small resource must both accompany
the final release; final application packages retain both exact resources plus
prominent notices. The user-venv preparation preserves the small resource,
licenses, modified sources, rebuild instructions and fixed full-source reference
under that venv's `share/vse-qt-ax-overlay/`.

Both archives were rebuilt twice locally with matching hashes and byte-for-byte
comparison. This is reproducibility of **resource packaging**, not a claim that
recompiling Qt yields identical signed Mach-O bytes. The builder requires the
fixed original candidate, source archive, patch and historical build files; it
does not compile, re-sign, download or install anything.

Authoring checks: 322 portable/safety/package-contract tests passed; one native
Windows launcher test was skipped on Linux. All workflows passed actionlint.
Both real resources passed the consumer's hash/inventory/source validation.
These results do not imply that the new source-prepare or integrated package
entry points have already run on the user's Mac or hosted Mac CI.

## Pure Python source preparation

Use the project's documented, user-created isolated venv and its exact macOS Qt
constraints. Close every VSE/Qt process using that venv before applying. Run with
the venv Python, not system Python. There is no sudo, global site-package change,
automatic pip installation, import-time patching, C++ worker or compiler step.

```sh
# Read-only inspection; this is the default.
.venv/bin/python scripts/prepare_macos_qt.py

# Authorized prepublication test: exact same locally supplied public-resource bytes.
.venv/bin/python scripts/prepare_macos_qt.py --apply --candidate-testing \
  --archive /absolute/path/vse-qt6.12.0-macos-arm64-ax772484-ps6.zip

# Only after publication and explicit production admission:
.venv/bin/python scripts/prepare_macos_qt.py --apply
```

Only native arm64 on macOS **26.0 or newer** is accepted. The official Cocoa
plugin declares 14.4, but 28 original PySide/Shiboken binding images declare
26.0; the whole runtime cannot honestly be advertised as 14.0/14.4 compatible.
The policy fixes the binding images, original Core/Gui, exact Qt/PySide/Shiboken
6.12.0 identity and the old/new Cocoa hashes. Future Qt versions must be reviewed
and re-tested; the overlay never blindly patches a new wheel.

The script rejects system Python, venvs with system-site-packages enabled,
escaping/symlink targets, unknown modified plugins, missing original RECORDs,
and a previously patched plugin without a matching local preparation receipt.
The original wheel RECORD bytes are preserved: the local overlay is explicitly
recorded, not falsely relabeled as an official wheel. Only the vendor Cocoa file
is atomically replaced; a failed mutation/receipt write restores its exact old
bytes. Retained license material and the preparation receipt are new owned files.
If the filesystem also refuses rollback, the error identifies the preserved
original-file recovery copy; it is not deleted or described as a successful setup.

An isolated child process then checks actual Cocoa/Core/Gui image paths, Qt
version and public accessibility activation. It uses only stdlib plus already
installed PySide, not PyObjC or developer tools. This load check is **not external
AX, full source GUI or OCR acceptance**. A failed load check leaves an explicit
failed probe receipt and does not silently switch libraries, disable AX, or claim
success. Do not use that environment until the failure is understood.

## Package integration API

```python
resource = prepare_resource(archive, fresh_cache_dir)
receipt = apply_to_stage(stage, staged_python, resource, candidate_testing=True)
notices = stage_notices(stage, resource, complete_source_archive)
```

The package-stage entry point is limited to hosted macOS arm64 CI, an interpreter
inside that temporary stage's runtime, and a not-yet-sealed application. The
caller must explicitly choose candidate testing while policy admission is
blocked. It returns a receipt and writes `runtime/vse-qt-ax-overlay.json`; it does
**not** alter pruning or bundle manifests. The main packager must update only
the Cocoa retained entry/byte total in its pruning manifest, preserve its original
record separately, then run unchanged verification, scan **all** runtime Mach-O
slices, create truthful Info.plist metadata, and seal the final package normally.
All official frameworks/bindings, application/OCR source and other vendor files
remain unchanged. No unsealed or already released ZIP is edited in place.

`verify_overlay(purelib, receipt)` rechecks the pinned runtime and preserved
vendor RECORDs after relocation/extraction. It does not turn preparation into
release permission. The final CoreML package still needs complete final-ZIP CI
and user-M5 acceptance with its own source/ZIP/plugin identities. Source GUI
needs its own real external-AX check; packaged GUI evidence cannot substitute.

For a Linux source-CI aggregate,
`validate_receipt_identity(receipt, require_native_load=True)` checks receipt
identity, fixed resource/runtime hashes, preparation-only flags, original RECORD
hash-map structure and the exact three own-venv native-image paths. It uses only
stdlib and does not need Mac files or execute Qt. The aggregate must separately
bind the original receipt bytes/hash and originating source-CI job. This portable
validator checks a bounded native-load **claim**, not independent execution or
external-client acceptance. The source command writes the original receipt to
`<sysconfig.purelib>/vse-qt-ax-overlay.json` for artifact preservation.

## Still blocked / independent acceptance

- Complete M5 diagnostic Open/Return/Cancel/repeated imports/selection/theme and
  restart flow, bound to original diagnostic ZIP and plugin hashes.
- A genuinely user-venv source GUI run with immediate external AX after Open.
- Final integrated CoreML ZIP: all relevant CI, runtime isolation, source/license
  completeness, actual macOS minimum, GUI/API/themes and AX probes.
- That exact integrated ZIP on the user's M5. CPU diagnostic acceptance cannot
  certify the CoreML package or imply GPU/ANE execution.
- Publish only under the user's final release authorization, then verify the
  actual public asset URLs/bytes. Mock HTTP tests do not count as publication.

Qt's patch remains explicitly identified as an unmerged upstream change, not an
official Qt binary/release. Prefer an officially released fixed wheel once its
actual source and native regressions have been checked. Do not erase provenance
or weaken security/manifest gates to make an outdated resource continue working.
