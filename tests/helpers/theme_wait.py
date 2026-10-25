"""Condition-based Qt animation/paint fence for GUI assertions (not an OS toggle)."""
import json
from pathlib import Path
import time

from PySide6 import QtCore


def animation_states(roots):
    states = []
    for root in roots:
        for animation in root.findChildren(QtCore.QAbstractAnimation):
            target = animation.targetObject() if isinstance(animation, QtCore.QPropertyAnimation) else None
            states.append({
                'class': type(animation).__name__,
                'owner': animation.parent().objectName() if animation.parent() else '',
                'target': type(target).__name__ if target is not None else '',
                'property': bytes(animation.propertyName()).decode() if isinstance(animation, QtCore.QPropertyAnimation) else '',
                'state': animation.state().name,
                'duration_ms': animation.duration(),
                'current_ms': animation.currentTime(),
                'current_value': str(animation.currentValue()) if isinstance(animation, QtCore.QVariantAnimation) else '',
                'end_value': str(animation.endValue()) if isinstance(animation, QtCore.QVariantAnimation) else '',
            })
    return states


class _PaintObserver(QtCore.QObject):
    """Observe normal event delivery without rendering from an event callback."""

    def __init__(self):
        super().__init__()
        self.painted = set()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Paint:
            self.painted.add(id(watched))
        return False


def wait_for_widget_animations(roots, timeout_ms=10000):
    """Await stopped animations and a naturally delivered, queued paint.

    Never call repaint/ensurePolished or force UpdateRequest delivery here:
    native style filters can recursively render and prevent even the deadline
    timer from running. update() schedules ordinary Qt event-loop painting.
    Hidden roots need no paint, but their animations must still stop.
    """
    roots = list(roots)
    loop = QtCore.QEventLoop()
    deadline = QtCore.QTimer()
    deadline.setSingleShot(True)
    deadline.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
    deadline.timeout.connect(loop.quit)
    probe = QtCore.QTimer()
    probe.setInterval(10)
    observer = _PaintObserver()
    for root in roots:
        root.installEventFilter(observer)
    completed = False
    stopped_passes = 0
    paint_requested = False
    expected_paints = set()
    started = time.monotonic()

    def check():
        nonlocal completed, stopped_passes, paint_requested, expected_paints
        if any(state['state'] != 'Stopped' for state in animation_states(roots)):
            stopped_passes = 0
            paint_requested = False
            return
        if not paint_requested:
            observer.painted.clear()
            expected_paints = {id(root) for root in roots if root.isVisible()}
            for root in roots:
                if id(root) in expected_paints:
                    root.update()
            paint_requested = True
            return
        if not expected_paints.issubset(observer.painted):
            return
        # Paint filters run before the paint handler. Confirm on subsequent
        # event-loop turns, never quit from inside an observed paint event.
        stopped_passes += 1
        if stopped_passes >= 2:
            completed = True
            loop.quit()

    probe.timeout.connect(check)
    deadline.start(timeout_ms)
    probe.start()
    try:
        loop.exec()
    finally:
        probe.stop()
        deadline.stop()
        for root in roots:
            root.removeEventFilter(observer)
    if not completed:
        evidence = Path(__file__).resolve().parents[2] / 'test-results/theme-wait-timeout'
        evidence.mkdir(parents=True, exist_ok=True)
        # Do not grab() on timeout: that too forces synchronous rendering of
        # the very widget whose event/paint processing may be unhealthy.
        states = {'elapsed_seconds': time.monotonic() - started,
                  'animations': animation_states(roots),
                  'paint_requested': paint_requested,
                  'stopped_passes': stopped_passes,
                  'expected_paints': sorted(expected_paints),
                  'observed_paints': sorted(observer.painted),
                  'widgets': [{'class': type(root).__name__,
                               'name': root.objectName(),
                               'visible': root.isVisible(),
                               'geometry': list(root.geometry().getRect())}
                              for root in roots]}
        (evidence / 'states.json').write_text(json.dumps(states, indent=2))
        raise AssertionError('Qt animations/queued paints did not settle: ' + json.dumps(states))
