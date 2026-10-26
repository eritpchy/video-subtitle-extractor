# macOS selected-table accessibility crash — investigation and isolated candidate

Status: **diagnostic candidate only; no production dependency change, package
replacement, Qt downgrade, tag update or release publication.** Local cloud
tests below are not native macOS acceptance.

Authoring verification: **54 passed, 2 native-only skips** for the probe,
build/evidence guards, runtime dependency resolver and Qt runtime tests on Linux
with Qt 6.12.0. The full 51,220,268-byte QtBase source archive and exact PS6 patch
were downloaded, SHA256-verified and applied successfully; both modified Cocoa
source hashes match the committed pins. macOS compilation and candidate runtime
gates remain **not run**: the first server attempt stopped at the build-tool
version guard, before source configuration.

### First server evidence: run 40254522060, source 315b10e

On hosted macOS 15.7.9 (24G830), arm64, both original-wheel Cocoa probes
(`selected-row` and `model-churn`) reported AX active and failed with
`table-cache-loss: live table interface was deleted` at the first selected-row
inspection. Both exited 1 with the explicit accessibility invariant, not a
SIGSEGV; the checked original plugin SHA256 matches the incident wheel. This is
a reproduced in-process native table-lifetime invariant, **not** a reproduction
of the user's macOS 26 external AX hierarchy SIGSEGV. Candidate comparison and
authorized final-app user-Mac acceptance are still required.

The server's `toolchain.json` matched Xcode 26.0.1/17A400, SDK 26.0 and CMake
4.1.2, but Ninja reported `1.13.0.git.kitware.jobserver-pipe-1`. The initial guard
incorrectly compared this binary version with the package version `1.13.0`.
The correction below pins both separately without changing any source, patch,
wheel, package or tool version. The old report did not record the selected
Ninja absolute path, so it does not establish that PATH selected a wrong tool.

Evidence ZIP SHA256:
`3361c994f1ca9a3e61fecb2f407f85f836135b13dc1d3aa70f953b5b8339e833`
(512,876 bytes; toolchain and both full stock probe logs retained).

## Exact incident and symbol identity

The two reported VSE 2.3.0 GUI crashes occurred on macOS 26.6.2 (25G83), arm64,
with PySide6/Qt/Shiboken 6.12.0, Fluent Widgets 1.11.3, Frameless Window 0.8.2
and PyObjC 12.2.2. The application launched, rendered and answered initial AX
reads. After accepting the native video file dialog, the next hierarchy read
crashed in the main-thread Cocoa accessibility path. OCR had not been started.
The two actions combined dialog acceptance and AX reading; they do not establish
whether an unattended/manual import without an AX reader also crashes.

The official [PyPI Essentials 6.12.0 metadata](https://pypi.org/pypi/PySide6-Essentials/6.12.0/json)
identifies the universal2 wheel and whole-wheel digest recorded in
[`native/qt/macos-accessibility-pins.json`](../../native/qt/macos-accessibility-pins.json).
A 1,080,506-byte HTTP Range extraction of its Cocoa plugin passed ZIP CRC and
produced:

| Evidence | Value |
|---|---|
| Plugin universal2 SHA256 | `e355b16024485dbacac13ea13a1ce46bca4cf682f8b7b698c557e5960185d636` |
| arm64 slice UUID | `028e2655-9fd8-3e6e-a861-d6cd79b760bc` — exact crash-report match |
| Reported image-relative PC | `0x85574` / 546164 |
| Objective-C method-table mapping | `accessibilitySelectedChildren` starts at `0x854b0`; PC is **+196** |
| Binary minimum OS / SDK | macOS **14.4** / **26.0** |

The Range extraction does **not** claim to have verified the entire wheel hash.
The isolated CI staging gate downloads the complete wheel and verifies the
publisher's whole-wheel SHA256 before using it.

## Source-level explanation and upstream state

VSE's `HomeInterface.open_file()` loads previews, adds task rows, and invokes
`TaskListComponent.select_task()`. The latter selects an entire three-column
row. There is no worker or widget destruction in that simple import path.

In [Qt 6.12.0 Cocoa source](https://github.com/qt/qtbase/blob/v6.12.0/src/plugins/platforms/cocoa/qcocoaaccessibilityelement.mm),
placeholder native table cells/rows refer to the parent table's accessibility
ID. Creating a real selected-cell representation replaces a placeholder.
The native wrapper cleanup can delete interfaces still owned/referenced by Qt's
table/cache; the selected-children enumeration can then use freed interfaces.
This is a strong source-level match to the **exact binary/method** above, not
an attribution to OpenCV or OCR. A stock-versus-candidate native reproduction
remains required before calling this application's defect fixed.

The cleanup was added by [upstream commit b1ed5f6](https://github.com/qt/qtbase/commit/b1ed5f656f064e553b33752f8e87d2f5b9553e38).
Direct source comparisons establish this boundary in the public 6.9 series:

| Qt source tag | Parent/interface-deleting cleanup |
|---|---|
| 6.9.0 | Absent |
| 6.9.1, 6.9.2, 6.9.3 | Present |
| 6.10.0, 6.11.0, 6.12.0 | Present |

The 6.8.2/6.8.3 public tags also lack this cleanup, but are not recommended as
alternatives: earlier native-element lifetime bugs exist. No public 6.12.1
tag or newer official PySide wheel was available when checked on 2026-10-26.

The official Gerrit API was inspected, not inferred from third-party reports:

* [QTBUG-149612](https://bugreports.qt.io/browse/QTBUG-149612) is the upstream issue.
* [765434](https://codereview.qt-project.org/c/qt/qtbase/+/765434) is an older
  synthesized-element guard. Qt's Apple maintainer pointed to 772484 instead
  on 2026-10-15. Do not combine the conflicting fixes.
* [772484, patch set 6](https://codereview.qt-project.org/c/qt/qtbase/+/772484/6),
  revision `bbe938de0b5863fe8edd77bea2dffb97c35e0db9`, separates native wrapper
  teardown from interface ownership. At this check it had Code-Review +2 and
  Sanity-Review +1, but **status NEW, not merged/released**. The complete patch,
  including its native test, applies cleanly to Qt 6.12.0.
* [777426](https://codereview.qt-project.org/c/qt/qtbase/+/777426) is a separate
  widgets-side stale-cache defensive change, also unmerged at this check. It is
  deliberately **not** included: this candidate changes only the Cocoa plugin.

## Why not silently pin all clients to Qt 6.9.0?

The official 6.9.0 wheel is useful as an **isolated causal control**, not an
automatic production fix. On Linux, its isolated overlay with current Fluent
Widgets completed 65 GUI/theme/timeline/icon/runtime tests (1 platform skip)
and `gui.py --smoke-test`, including video preview and all pages. This proves
Python/API compatibility only; it does not prove native Cocoa safety.

Qt's [security advisory list](https://wiki.qt.io/List_of_known_vulnerabilities_in_Qt_products)
shows that 6.9.0 lacks later fixes, including SVG memory-safety issues and
QtNetwork/QtXml issues. A focused VSE call-path audit found:

| Module / example advisories | Current VSE exposure found | Remaining limitation |
|---|---|---|
| QtSvg: CVE-2025-10729, CVE-2026-6210 | Fluent/Frameless render shipped SVG icons; user videos are decoded by OpenCV and passed as raw RGB to `QImage` | Module is loaded; bundled/trusted inputs reduce, not eliminate, risk |
| QtXml / XML reader: CVE-2026-19248, CVE-2026-78253 | Icon recoloring uses `QDomDocument` with shipped icon resources | Malicious replacement resources or future input paths remain a concern |
| QtNetwork HTTP/2: CVE-2025-5991 and later fixes | Application update requests use Python `requests`; no application QtNetwork request path found | This is a focused call-site audit, not a proof of unreachable transitive code |
| Qt Quick / WebEngine / PDF | No application QML/Quick execution; WebEngine/PDF/Addons excluded by the native runtime selector | Do not generalize this to arbitrary Qt applications |
| OpenSSL certificate path CVE-2025-14575 | Advisory explicitly excludes macOS | Not a reason to roll back Windows/Linux |

No security exception or old-version production pin is introduced by this work.

## Bounded server-side candidate

The independently triggered workflow
[`macos-accessibility.yml`](../../.github/workflows/macos-accessibility.yml)
does not run from or feed the release workflow. Its push trigger requires both
the exact `fix/restore-release-matrix` branch and changes to its seven explicitly
listed AX workflow/script/pin/constraint/test paths. Other branches, tags,
documentation-only and unrelated release changes do not trigger it. Manual
dispatch remains declared, but [GitHub requires the workflow on the default
branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
before dispatch can work; this investigation does not change the default branch.
It uses a GitHub-hosted arm64 macOS runner; the build/stage script refuses
desktops and self-hosted executors.

1. Pin official QtBase 6.12.0 source archive, its published SHA256, exact Gerrit
   PS6 revision and decoded patch SHA256. Reject changes to either input.
2. Pin Xcode 26.0.1 build 17A400, SDK 26.0, macOS minimum 14.4, CMake 4.1.2 and
   Ninja Python package 1.13.0 and its binary's exact
   `1.13.0.git.kitware.jobserver-pipe-1` version separately. The
   [publisher's distribution](https://pypi.org/project/ninja/1.13.0/) bundles a
   Kitware fork, not a binary whose version is simply the package version.
   The locked Mac wheel (`fa2a8bfc62e31b08f83127d1613d10821775a0eb334197154c4d6067b7068ff1`)
   was downloaded and hash-verified; both Mach-O slices contain the exact
   `.git.` version above, and executing the locked Linux wheel agrees. Do not
   substitute the publisher README's source-tag `.gd74ef.` for the built binary
   version. Record package versions, PATH-resolved executable paths and SHA256;
   reject commands not owned by the installed pinned package as well as version
   mismatches. The source wheel's own Mach-O records justify SDK/minimum-OS
   choices. If the hosted image lacks this toolchain, **fail**, do not silently
   select another one.
3. Use normal upstream QtBase configuration, accessibility **ON**, then build
   only `QCocoaIntegrationPlugin`. QtCore/QtGui build dependencies may be built;
   their output is **never copied into the runtime**. No entire Qt/PySide rebuild
   or WebEngine build is required. Cold build duration is not yet measured;
   the diagnostic job has a 100-minute process budget, not a promise of success.
4. Check arm64, minimum OS, SDK, exact Cocoa plugin metadata, runtime library
   paths and every imported QtCore/QtGui symbol against the **official wheel's**
   framework exports. Reject Homebrew, build-tree and extra Qt dependencies.
5. Stage just one `.dylib` inside `work-dir/candidate-venv`, never a release
   archive. Compare the entire PySide file inventory before/after. Keep the
   original wheel and distribution `RECORD` unchanged; explicitly record the
   one expected vendor-file modification in `staging.json`.
6. Check actual dyld image paths to prove that QtCore and QtGui came from the
   official wheel, not the newly built source dependencies. Compare crash-isolated
   stock and candidate probes, then run real Cocoa GUI/API/theme tests and render
   all pages/video preview. Require both a targeted stock failure and candidate
   success; a non-reproducing baseline is **inconclusive**, not a fixed-regression
   pass.

Qt explicitly gives [no general QPA ABI compatibility guarantee](https://doc.qt.io/qt-6/qpa.html).
The same-version checks above narrow and test the risk; they do not establish
that all private layouts or native window paths are equivalent. Build success
alone is insufficient. A missing export, wrong image load, native probe failure,
unavailable toolchain or inconclusive baseline blocks this candidate without
falling back to an entire rebuilt Qt runtime or an old Qt version.

### Rebuild commands (hosted Mac CI only)

The workflow installs the selected, untouched native-GUI wheels with the separate
diagnostic constraints file and creates `build/macos-qt-ax/candidate-venv`.
Then it runs:

```sh
python scripts/build_macos_qt_accessibility.py probe --phase stock \
  --work-dir build/macos-qt-ax --python build/macos-qt-ax/candidate-venv/bin/python
python scripts/build_macos_qt_accessibility.py build --work-dir build/macos-qt-ax --jobs 3
python scripts/build_macos_qt_accessibility.py stage --work-dir build/macos-qt-ax \
  --python build/macos-qt-ax/candidate-venv/bin/python --wheelhouse build/macos-qt-ax/wheelhouse
python scripts/build_macos_qt_accessibility.py verify-runtime --work-dir build/macos-qt-ax \
  --python build/macos-qt-ax/candidate-venv/bin/python
python scripts/build_macos_qt_accessibility.py probe --phase candidate \
  --work-dir build/macos-qt-ax --python build/macos-qt-ax/candidate-venv/bin/python
```

`finalize` additionally requires successful GUI JUnit and smoke logs. Its receipt
always keeps `production_integration_allowed: false`, external AX client coverage
false, and user-macOS acceptance false. These are separate review/acceptance steps.

### Native probe coverage and limits

[`macos_accessibility_probe.py`](../../scripts/macos_accessibility_probe.py)
creates only its own disposable three-column table. Cocoa mode directly queries
Qt's native accessibility object through PyObjC, checks selected-cell count,
titles and roles, retained table ID, keyboard selection, model removal/reset/
reinsertion, repeated reads and disposal. Each scenario runs in its own process.
It does not inspect another application, request OS accessibility access, change
OS preferences or disable AX. Public `--mode qt` tests never claim native success.

The native Cocoa probe has **not run in the Linux authoring environment**. A
missing PyObjC selector, inactive bridge or environment error must be reported
as a blocker, not counted as the reproduced crash. The native probe is not an
external AppKit/HIServices hierarchy-copy test and is not spoken VoiceOver QA.

## Redistribution and eventual acceptance

Candidate artifacts carry the original source archive, exact patch, modified
Cocoa source, original `LICENSES/`, a clear modification notice and the build
recipe. Preserve Qt's applicable LGPL notices/source availability and the
[Cocoa BSD attribution](https://doc.qt.io/qt-6/qtgui-attribution-cocoa-platform-plugin.html).
This engineering packaging plan is not a substitute for release-license review.

Before any production integration: review CI evidence for the exact commit and
candidate hash, then use an **authorized** native Mac session to test the final
application: Open/Cancel/repeated import, first selected row, repeated external
AX hierarchy reads, selection and task deletion/replacement, preview, theme/page
switches, close/reopen and actual OCR. Retain accessibility content and controls;
an empty selection, disabled AX or removed task table is not a successful fix.
Desktop approval remains a separate prerequisite and is not bypassed by this job.
