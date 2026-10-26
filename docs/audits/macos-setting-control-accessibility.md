# macOS mode/theme control accessibility

## Separate from the Qt Cocoa cache defect

The native-file-dialog / immediate external AX crash is tracked by the Qt
772484 overlay audit. The later M5 observation was different: the application
stayed alive, but the custom mode/theme popup could not be selected through the
app-specific AX client or keyboard. This does not prove ordinary mouse input is
broken, nor that a popup missing from a main-window screenshot is invisible.
Neither issue is a reason to disable accessibility.

## Upstream check, 2026-10-26 UTC

* [Official PyPI PySide6-Fluent-Widgets](https://pypi.org/project/PySide6-Fluent-Widgets/)
  and its JSON API report **1.11.3**, uploaded 2026-08-01 02:46:13 UTC, as latest.
  PyQt-Fluent-Widgets is also 1.11.3, but is not the PySide6 distribution to install.
* The [PySide6 branch version commit](https://github.com/zhiyiYo/PyQt-Fluent-Widgets/commit/d6f5a01f7f3fe285c6900e476349467810267839)
  and [changelog](https://github.com/zhiyiYo/PyQt-Fluent-Widgets/blob/PySide6/CHANGELOG.md)
  concern tooltip shadows, disabled button styles, and Pivot positioning, not this
  control's keyboard/AX interaction.
* The freshly downloaded [PySide6 combo implementation](https://raw.githubusercontent.com/zhiyiYo/PyQt-Fluent-Widgets/PySide6/qfluentwidgets/components/widgets/combo_box.py)
  matches the installed 1.11.3 file byte-for-byte: 16,713 bytes, SHA-256
  `3c8c90df16da41e6647c2580b3fc1317bd167cd8b094829b0eb9fc96a2c24f27`.
  Its non-editable ComboBox is a QPushButton and opens the RoundMenu only from
  mouseReleaseEvent. Actual Qt Press / click activation does not open that menu.
  ModelComboBox uses the same button/custom-menu approach.
* [PR 1121](https://github.com/zhiyiYo/PyQt-Fluent-Widgets/pull/1121), created
  2026-10-17 and last updated 2026-10-18, was **open, unmerged** when checked.
  Head `39c5de4a2da9505c5a6d42a6503d23baa8b3a058` targets master/PyQt5 and
  changes ten files (+804/-638) for popup positioning, motion and styles.
  Its ordinary ComboBox still opens from mouseReleaseEvent; it adds neither
  keyboard activation nor a QAccessible implementation. This is not an available
  fixed PySide6 release, and importing that wider unmerged change is unjustified.
  Some GitHub release/commit endpoints returned 403; successful PyPI, raw source
  and PR metadata/diff reads support the narrower findings above, not a claim
  that no related issue could exist anywhere upstream.

## Bounded application fix

Only macOS's OCR mode and theme setting cards replace their child with standard
Qt QComboBox. The Fluent card, labels, layout slot, option order/text/userData,
configuration item and callbacks remain in place. Other platforms and other
combos are untouched. No third-party installed files, popup flags or global
accessibility factories are changed.

Setup copies the current value before connecting the existing card callback;
it neither sets nor saves configuration. The obsolete button is disconnected,
hidden and deleted through Qt. Standard Qt owns mouse/keyboard behavior, menu
ownership, focus and the ComboBox accessibility role / ShowMenu action. The
label remains its buddy; description includes the field label because Qt's
accessible combo exposes its selected option as Name/Value. Palette updates
follow the existing effective theme signal (including Auto), without changing
the saved preference. An unfocused combo ignores wheel input so scrolling the
settings page does not change OCR mode accidentally.

## Validation boundary

`tests/test_macos_setting_controls.py` uses real Qt key/mouse events and the Qt
accessible action: all three modes, theme selection, tab focus, popup selection
and Escape, single activation/save, initialization without writes, fresh-process
persistence, disabled items, and effective-theme palette changes. The Cocoa
popup action test is explicitly not substituted by offscreen results: its native
external input is a separate Mac acceptance requirement. A standard popup is
still a separate window; this patch does not promise inclusion in a main-window
screenshot.

Before production acceptance, verify the **exact final source/package** on the
user's Mac with external AX, real mouse/keyboard selection, light/dark/system
appearance and restart persistence. These tests do not waive the Qt overlay's
blocked production admission or promote the old CPU diagnostic into a CoreML
production package.

## First native CI palette assertion

Run 40311353293 / job 121533546794 passed 11 control tests, explicitly skipped
the non-Cocoa popup test, and failed whole-QPalette equality on macOS.
The saved XML's abbreviated repr
does not identify the differing roles; the preceding Base/lightness check passed.
Do not infer a particular native color mismatch from that abbreviated output.

The pinned QtBase 6.12.0 source clarifies the contract:
`src/gui/kernel/qpalette.cpp` compares all brushes (not currentColorGroup), but
its debug representation prints only explicitly resolved roles.
`src/widgets/kernel/qwidget.cpp` resolves unspecified roles against class/parent
palettes. `src/gui/painting/qcolor.cpp` also distinguishes HSV/RGB encodings even
where display RGBA matches. These are not interchangeable with an appearance
contract for two different widget classes.

The regression now checks exact display RGBA, solid brush and identity transform
for all eight application-assigned roles in **Active, Inactive and Disabled**,
independently against their expected light/dark colors. It checks both combo and
popup view, not just the combo. Real Qt resolution/HSV-to-RGB examples demonstrate
why whole-palette identity is not required. Every native run saves all roles,
RGBA, floating components, color specification, brush style, explicit bits,
resolve mask, current group, platform and Qt backend in
`test-results/macos-setting-controls/palette-states.json` before asserting.
Any assigned display-color or brush mismatch still fails. Product palette code
is unchanged. Native rerun and M5 appearance checks remain required; this test
correction alone does not establish the cause of the earlier Cocoa difference.

## b012 Server focus-test precondition correction

The original standard Mac run 40345271680/job 121651122119 failed only
`test_tab_focus_includes_the_standard_combo` (`hasFocus() == False`, 0.054 s).
Its raw JUnit did not record activation, starting focus, or the platform Tab
policy, so it cannot establish the precise Cocoa cause. The same test passed
on Windows 2022/2025. All 23 new preview lifecycle tests passed, without skips,
on each of those three platforms. The evidence archive is 1,027,921 bytes,
SHA-256 `f308d334a71d79ddd2bf9a8aa6a7fef10b70f7505f6343586687c77cf5f21734`.

The fixture previously requested activation, processed events once, called
`before.setFocus()`, then immediately sent Tab without verifying either starting
condition. Qt explicitly provides
[`qWaitForWindowActive`](https://doc.qt.io/qt-6/qtest.html#qWaitForWindowActive)
for tests requiring asynchronous window activation. Its
[6.12 focus traversal](https://github.com/qt/qtbase/blob/v6.12.0/src/widgets/kernel/qapplication.cpp#L1829-L1858)
uses either the TabFocus or StrongFocus mask according to the platform Tab
policy. The production combo already has StrongFocus, satisfying both masks;
there is no evidence here justifying a system keyboard-setting override.

The test-only correction waits once, with a bound, for the actual active window
and initial field focus. Each single Tab/Shift+Tab event must then reach the
expected real widget, forward and backward, without directly focusing the combo
or changing a configuration value. Every stage records UTC/elapsed time,
application/window activity, focus window/widget, focus policies, and the
platform Tab behavior in `macos-setting-controls/focus-states.json`, including
on failure. It does not retry the navigation until green, skip an assertion,
change OS keyboard preferences, or modify production controls. A delayed-focus
regression verifies the wait helper cannot succeed for the wrong widget.

Local offscreen success verifies the fixture and real Qt event contract only.
A new Cocoa run and final exact-package M5 keyboard/AX checks are still required;
this change does not retroactively classify b012's failure as harmless.
