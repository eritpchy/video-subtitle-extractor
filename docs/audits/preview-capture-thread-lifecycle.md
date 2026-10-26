# GUI preview capture lifetime (2026-10-26)

## Failure and evidence boundary

The M5 acceptance run of source `2717ae4675879d17e6c759bb4f1fd6c778003ae5`
used final Native CoreML ZIP SHA256
`e2e53504df6697bc78ce2eaaa3f237477ef95d8c9bc33856f84ab4626d1f3928`.
CoreML fast/accurate/auto generated the expected SRT. The explicit CPU fast run
also generated that SRT, but PID 11042 subsequently crashed. **CPU fast GUI
continuity failed**; inference output is not a successful GUI acceptance.

The evidence archive (`libfile_d8b527a54b4081918c59be70bf7e7d19`, 651866 bytes,
SHA256 `22d1d3a125e24619a0bf0a330a446c930792981ac4f3bfd834b60c5775012f29`)
contains the crash summary, initial identity, screenshots, AX output and SRTs.
The original macOS report is `python3.12-2026-10-26-212144.ips`, 84518 bytes,
SHA256 `9716c9865b3380e715aae373b83add070a2ae39793d16ab7478a75dfa9be4d08`.
Its full original bytes remain on the authorized Mac; the multi-file Library
transfer was unavailable and was not bypassed. A read-only inspection there
reported 20 threads and these simultaneous stacks:

- Thread 0 / id 57653, triggered, main queue: libavcodec offsets 1242816 and
  1245068 → `CvCapture_FFMPEG::seek` +464 → `setProperty` +156 →
  `cv::VideoCapture::set` +68 → Python/PySide → `QAbstractSlider::setValue` +324
  → queued QObject event delivery.
- Thread 9 / id 102049, not triggered: pthread join → libavcodec offset 3983688
  → `CvCapture_FFMPEG_proxy::__on_zero_shared` +40 →
  `cv::VideoCapture::release` +112 → Python → `thread_run` → pthread start.
- cv2 UUID `7898dff1-c61c-3b75-a7a9-d9215a5c7c14`; libavcodec UUID
  `fb2a7943-244f-327e-b570-99ee8ec2a5d5`.

The native report proves simultaneous seek/release stacks, not a Python object
identity. The source explains the shared object: the background task's `finally`
released `self.video_cap` immediately after posting terminal progress; the GUI
processed that progress by setting its slider, synchronously seeking/reading
`self.video_cap`. Startup also loaded/replaced this capture and mutated widgets
from the background thread. This is a capture ownership defect, not evidence of
a CPU inference defect or a recurrence of the Qt Cocoa AX-cache defect.

## Narrow fix

- All preview construction, read/seek, replacement and release, task-list state,
  option snapshots and widget changes belong to the GUI thread. Guard the entry
  points. Block slider signals only while setting up a newly decoded preview;
  retain the existing slider/AB range semantics.
- A worker gets a copied per-task input specification and an independent job
  cancellation context. It only starts/joins the extraction process, receives
  IPC and emits explicitly queued, job-tagged events.
- Resolve the current row by original Task identity. Ignore stale events after
  Stop→Run, task deletion, or importing the same path as a new task. Track the
  monotonic progress floor on the job, not a mutable row number.
- OCR `isFinished` does not end the process or batch. Drain the child's FINISH
  stream before posting process completion, preserve an ERROR even for exit
  code zero, and fail on missing FINISH or reader/callback failure.
- Main-window close explicitly shuts down HomeInterface before child widgets
  die. Invalidate jobs and serialize signal emission with shutdown, so a late
  worker does not emit through a destroyed native QObject. Disconnecting alone
  would not discard events that were already queued.

Qt requires QWidget work to remain in the main thread and supports queued
cross-thread result delivery ([Qt threading documentation](https://doc.qt.io/qt-6/threads-qobject.html)).
Previously queued deliveries can remain after disconnect
([QObject::disconnect](https://doc.qt.io/qt-6/qobject.html#disconnect)).

No OCR provider/model, output processing, Qt binary, AX policy or release guard
changes are part of this fix. The existing one-second between-task delay remains
an owned GUI timer instead of a worker sleeping while touching shared UI state.

## Regression scope

`tests/test_home_preview_lifecycle.py` exercises real Qt event delivery and:

- A completion emitted while a slider/capture seek is held behind a deterministic
  barrier: release occurs only after the GUI operation returns. Instrumented
  capture and Python widget methods record thread violations, rather than hiding
  an assertion in a swallowed worker exception.
- Terminal progress vs authoritative completion, duplicate Run, Stop→Run with old
  progress/error/completion, two-task transition, deleting a preceding row,
  replacing the same file path, queued callbacks at close, parent-window shutdown,
  and failed first decode cleanup.
- Three repeated generated MJPG-video runs through real OpenCV VideoCapture,
  slider seeks, completion and reload; inference is controlled in this test.
- Actual process-orchestration/IPC wiring with a controlled process endpoint:
  final log/progress drain, ERROR plus zero exit, absent FINISH, and callback
  delivery failure. Independent reader tests cover idle stop/wakeup and a real
  spawned producer sending 1 MiB after a failed callback; its feeder must exit
  without reporting successful delivery. Shutdown also terminates an actual
  owned waiting Python child (not an OCR acceptance run).

These are source/Server regression checks, not a replacement for the new final
ZIP's M5 GUI run. Final CPU/CoreML modes, finish/close/reopen continuity, native
file Open→immediate external AX, theme choices and persistence still require
fresh acceptance on the exact rebuilt candidate. Historical failed or false
acceptance records must not be rewritten.
