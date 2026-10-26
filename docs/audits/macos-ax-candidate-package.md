# AX-DIAGNOSTIC: separate final Mac application candidate

**Not a released Qt binary, not an official VSE update, and not production
integration approval.** The original package is unchanged. Server in-process
AX success is not external-client/user-Mac acceptance. Do not replace a user's
existing application with this candidate before explicit review and authorized
testing. Do not merge the diagnostic branch's trigger exclusions into the main
repair branch.

Authoring checks: 185 portable archive, deployment-floor, overlay, AX-build and
release-contract tests passed, including a small real ZIP/manifest/pruning
round-trip. All workflow files passed actionlint 1.7.7. The actual preserved Qt
candidate bytes/licenses/evidence were independently hash-checked and passed the
new input validator. These are not final-ZIP Server results; that separate
workflow still needs to execute against the pinned 517,468,847-byte base archive.

## Fixed inputs and identity

`native/qt/macos-ax-package-pins.json` binds the exact accepted Mac Native ZIP
from source `59749ccc02e369f374c69846b2bde6279084577d`, run `40259119763`, artifact
`12623898599`, plus Qt candidate build `561de09ed45180362732725d24640862d3fa1ceb`,
run `40260642504`, artifacts `12624219589` / `12624434196`. No latest-run search,
unreviewed fallback URL or unsigned source substitution is supported. If an
artifact expires, stop and obtain a newly reviewed preserved input; never select
another successful run automatically.

The replacement Cocoa plugin SHA256 is
`d23ea2a1e3201ec85f4fd35ee8bff32d7217ce6b0bbb51058339f1abca612941`.
The only modified vendor runtime file is
`PySide6/Qt/plugins/platforms/libqcocoa.dylib`. Official QtCore/QtGui, Python,
Shiboken, all other PySide files, every original vendor RECORD and all VSE/OCR
application source bytes remain unchanged. Original RECORD describes the
upstream wheel; this explicit local overlay is not misrepresented as a wheel
published or signed by Qt.

The new candidate separately seals the changed plugin in
`runtime/inference-prune.json` (only its one retained entry and byte total) and
`bundle-manifest.json`; both original manifests are preserved with the provenance.
No verifier is weakened. The `.app` Info.plist minimum OS is also corrected to
the highest declared minimum over **all loadable Mach-O slices in this package**.
Every other app-wrapper file must remain unchanged.

The original official Qt 6.12.0 Cocoa arm64 binary and the replacement both
declare macOS **14.4**, despite the upstream wheel filename's `macosx_14_0` tag.
The candidate never claims macOS 14.0 compatibility. `minimum-system.json` lists
every inspected image and the limiting files; `LSMinimumSystemVersion` and the
diagnostic receipt must agree with its maximum. This is a static declared-floor
audit, **not** an execution test on every supported old macOS version. No Mach-O
minimum-version commands are edited to manufacture compatibility.

## Build and final ZIP checks

Only the GitHub-hosted Mac arm64 job on `fix/macos-accessibility` can run:

```sh
python scripts/package_macos_ax_candidate.py download
python scripts/package_macos_ax_candidate.py package
python scripts/package_macos_ax_candidate.py verify
```

The archive helper validates all member names, types, size/count bounds,
case-insensitive/Unicode collisions and parent conflicts before extraction.
Original input hashes/receipts are verified before any runtime execution.
The unchanged original package verifier then executes the **final ZIP's**
launcher, real fast/accurate/auto OCR, video/multiline issues, GUI smoke and
dependency-isolation checks. Fresh final-ZIP extraction additionally runs native
Cocoa selected-row/model-churn probes and verifies actual dyld image paths and
hashes. A final diagnostic receipt is emitted only after all pass, while keeping
`production_integration_allowed`, `external_ax_client_tested` and
`user_macos_26_6_2_acceptance_passed` false. Runtime tests do not install dependencies
or borrow host Qt. Do not set DYLD search overrides or disable accessibility.

## Qt licensing, sources, modification and rebuilding

The candidate includes the complete original QtBase 6.12.0 source archive,
the exact Gerrit 772484 PS6 patch, all original Qt `LICENSES`, modified Cocoa
source with its copyright notices, `MODIFICATIONS.txt`, build pins and build
script, and the original CI acceptance/provenance. The source archive SHA256 is
`a951bd163c7b80fc6b8c88d7668fb56abf91c152373e13c10666763238131307`; the decoded
patch SHA256 is `fbc6e92fb2e27c220821cbaaf8e16579cb77dbc83eb7acffe8ae3df7ed3a503b`.
Do not replace these with only a moving upstream download link. Preserve the
LGPL-3.0/GPL-3.0 texts and Cocoa's Apple BSD attribution. Qt describes its
[open-source licensing obligations](https://www.qt.io/development/open-source-lgpl-obligations)
and [Cocoa attribution](https://doc.qt.io/qt-6/qtgui-attribution-cocoa-platform-plugin.html).
These retained materials are not a blanket legal certification of all software
in the surrounding application.

For reproducible build inputs, use the matching source checkout and
`scripts/build_macos_qt_accessibility.py` with the pinned Xcode 26.0.1/17A400,
SDK 26.0, CMake 4.1.2, Ninja package 1.13.0 / binary
`1.13.0.git.kitware.jobserver-pipe-1`, arm64 and minimum macOS 14.4. The complete
configure command and source/patch hashes are in `candidate.json`; build only
`QCocoaIntegrationPlugin` plus its dependencies. Keep build-tree RPATHs for rcc;
relocate only the copied plugin to `@loader_path/../../lib` and ad-hoc sign that
plugin. No claim of bit-for-bit reproducible code-signature/UUID bytes is made.
Any rebuilt binary must obtain its own ABI, runtime and AX evidence before use.

The library is dynamically linked and its corresponding source is supplied.
Users may study, modify and replace it; the application source and checksum
format are available. Replacing a library in a *new copy* requires regenerating
that copy's explicit pruning/bundle checksum manifests, not bypassing the
verification code. The original manifests and provenance must not be relabeled
as attesting a newly modified binary. Normal operating-system code-signing and
permission requirements still apply; these instructions do not authorize
Gatekeeper bypasses, accessibility permission changes or operating on another
application/user's data.

## Still required on the user's Mac

After the parent task obtains exclusive GUI approval, open this exact separately
named final ZIP application on macOS 26.6.2 and repeat native Open confirmation
followed immediately by the real external AX hierarchy read. Cover repeated
imports, Return/button acceptance, Cancel/Escape, row selection, preview/seek,
light/dark/system themes, three OCR modes and clean close/relaunch. Verify AX
trees remain nonempty and usable, not merely that the app avoids a crash.
Record the candidate ZIP/plugin hashes, process identity, OS build and external
client identity. Do not mark those checks passed from Server in-process probes.
