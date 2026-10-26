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
