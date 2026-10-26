Server-only NSApplication activation diagnostic (not a product fix)
================================================================
Source target: aeb41d02cfd1257711236c08fc223156c35cc5f6
Original failure: standard run 40365764848 / Mac job 121724040980.
Original raw: libfile_16d8f03ea3bc8191b678ae956419b547, 543632 bytes,
SHA256 f1355b4118331e7112d847cb5d797a19b06f6e5ab534838a206c625ae2088499.

Scope
-----
Only an authorized GitHub-hosted macos-26 ARM64 Server runner. Do not run this
on the user's Mac (locked or unlocked). No workflow, source, requirements,
system preference, accessibility permission or activation-policy changes.
No process/window enumeration, other-app activation, or foreground strategy
changes. Only read the current process's NSApp, caller Quartz session's UID /
on-console / login-complete fields, and the single frontmost application's PID.
The script checks the console UID before requesting activation.

There is no new package installation. Original aeb job log lines 479/539/719
and installed-packages line 1058 prove existing official pyobjc-core,
pyobjc-framework-Cocoa and pyobjc-framework-Quartz 12.2.2. The probe requires
exactly those versions, pytest 8.3.5 and the existing Cocoa Qt 6.12.0.
Missing modules or mismatched versions are errors, not reasons to install or
substitute a dependency automatically.

ABI and implementation
----------------------
Use the existing official PyObjC bridge rather than handwritten objc_msgSend
ctypes. Apple documents different arm64 variadic calling conventions and BOOL
representation. Before any activation call, the probe validates selector
signature and bridge metadata as exactly void(id, SEL), no explicit arguments.
This validates the intended call shape; actual AppKit execution remains NOT
RUN in cloud Linux. Official bridge handles arm64 return/argument ABI.

One original test per process. Verify pinned HEAD, all tracked files clean,
and original test file equal its Git blob BEFORE importing project tests.
Git commands explicitly set GIT_NO_LAZY_FETCH=1, disable fsmonitor, and never
fetch/reset. Missing local objects fail. Recheck HEAD/bytes/clean status after
the test and store both identities. Output must be a new directory; never
replace earlier evidence. Launch Python with explicit -B (also enforced).

The plugin wraps only the test fixture show() and tearDown(), NOT either test
method. After original show/activateWindow/processEvents it records state,
requests this process's modern NSApplication.activate() exactly once, records
again, and returns immediately to the original test. The original 5000 ms
qWaitForWindowActive, initial focus assertion, true forward/backward Tab key
assertions and configuration checks remain unchanged. No added wait/retry,
setActiveWindow, synthetic internal focus, direct combo focusing, or fallback.
Original teardown always runs, even if diagnostic recording raises.
A failed activation, ABI mismatch, skipped test, empty collection or source
postcondition mismatch cannot produce a successful diagnostic.

Execution by the existing authorized CI executor only
----------------------------------------------------
The previous aeb hosted job has ended; do NOT assume its temporary runner or
installed environment still exists. This archive is probe source, NOT a wired
Server job. The parent must coordinate one separately authorized small Server
job that checks out exact aeb and prepares the same pinned test environment
(with no new dependencies). On that Server use the prepared checkout and
installed environment; do not run the full matrix for this diagnostic. Do not set
OS preferences or fake runner variables to satisfy guards. QT_QPA_PLATFORM
must already be cocoa. The two commands below are separate processes, each
with exactly one allowed original test and at most one request:

  python -B /ABS/PROBE/server_focus_probe.py \
    --checkout "$GITHUB_WORKSPACE" --case wait \
    --output "$RUNNER_TEMP/focus-probe-wait"

  python -B /ABS/PROBE/server_focus_probe.py \
    --checkout "$GITHUB_WORKSPACE" --case tab \
    --output "$RUNNER_TEMP/focus-probe-tab"

Capture the complete stdout/stderr and exit code separately for each once.
Even if the first fails, a separately authorized second original test may
still be executed once; do not retry either case until green. Keep each
activation-report.json, junit.xml and original test-results/macos-setting-
controls/focus-states.json (when emitted), full log and actual runner metadata.
Always upload reports on failure. No automatic publication or production flags.

Interpretation
--------------
A pass would establish only that the exact original test passed on that
Server with one explicit app-activation request. Because these are isolated
processes, it would NOT by itself prove why the earlier full-suite process
became inactive, eliminate all ordering interactions, prove M5 GUI/AX behavior,
or qualify a production change. A refusal/inactive result is preserved and
must be inspected as runner/session/native activation evidence, not turned
into a skipped navigation test. Both original 5-second failures preceded Tab.

Validation already performed
----------------------------
Cloud Linux, only 13 offline stdlib unit tests passed (offline-tests.log):
exact Server guards, exact ABI shape rejection, own-session gating, one-shot
including native exception/no retry, original test identity and cleanup, original
assertion failure propagation, strict collection, raw source identity, explicit
no-lazy-fetch and non-Server rejection/evidence retention. These are NOT native
Cocoa tests. No PyObjC, NSApp activation, real Qt Tab or actual pytest-plugin
execution has yet run in this replacement cloud environment.

Official references
-------------------
https://developer.apple.com/documentation/appkit/nsapplication/activate()
https://developer.apple.com/documentation/apple-silicon/addressing-architectural-differences-in-your-macos-code
https://pyobjc.readthedocs.io/en/latest/api/module-objc.html#objc.selector.signature
https://pyobjc.readthedocs.io/en/latest/api/module-objc.html#objc.selector.__metadata__
https://developer.apple.com/documentation/coregraphics/cgsessioncopycurrentdictionary()
https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPMultipleUsers/Concepts/FastUserSwitching.html
https://github.com/qt/qtbase/blob/v6.12.0/src/plugins/platforms/cocoa/qcocoawindow.mm#L1739-L1744
https://github.com/qt/qtbase/blob/v6.12.0/src/plugins/platforms/cocoa/qcocoaintegration.mm#L124-L142
https://github.com/qt/qtbase/blob/v6.12.0/src/plugins/platforms/cocoa/qcocoaeventdispatcher.mm#L494-L527
https://raw.githubusercontent.com/pytest-dev/pytest/8.3.5/src/_pytest/unittest.py

CI wiring addition (2026-10-26)
-------------------------------
The separate workflow is .github/workflows/macos-focus-activation-diagnostic.yml.
It is limited to fix/macos-focus-activation and its own workflow/diagnostic paths.
The subject is checked out into source-aeb at the exact original SHA, separately
from the diagnostic driver. Source sparse checkout is non-cone and excludes ONLY
backend/models, retaining all other original GUI resources/config/language files.
The two original tests import config/Qt/theme helpers, not Home/OCR. Verified aeb
backend/config.py reads only language INIs and GUI config, not model assets.
This also avoids two malformed legacy LFS model blobs seen in the original job.
No .gitignore, LFS filter, source or git-status identity guard is relaxed.

The job prepares original requirements.txt with 257 exact observed installed
version constraints, recorded verbatim with raw log provenance. It does not
introduce extra packages or fetch model assets/VideoSubFinder, run OCR, or build
this project's native components. Ordinary pip behavior is retained: pysrt 1.1.2
is a pure-Python sdist on PyPI, so adding --only-binary would break equivalence.
Two test commands are sequential, distinct processes, with original exit codes;
the second runs once even if the first fails, provided setup succeeded and the
job was not cancelled. Log-pipeline failure is not swallowed. Reports upload
always. A separate strict patch excludes only this diagnostic branch's push
from main ci/release; all original PR/manual/other-branch coverage remains.

19 combined offline contracts passed, including workflow shape, source checkout,
raw observed constraint transcription, a real controlled Git sparse checkout,
and bash syntax of every run block. Native
Server execution remains not run. Full actionlint of the diagnostic tree is a
separate handoff validation by the integration owner/authorized CI writer.
