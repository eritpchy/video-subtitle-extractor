"""Condition-based Qt animation/paint fence for GUI assertions (not an OS toggle)."""
import json
from pathlib import Path
import time

from PySide6 import QtCore, QtWidgets


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


def wait_for_widget_animations(roots, timeout_ms=3000):
    """Await animation states, then drain queued styles and repaint synchronously.

    The parent FluentWindow and its translucent cards animate independently.
    A nominal duration plus sleep is not a completion condition on a busy native
    runner. The short timer is a condition probe, not a chosen settling delay.
    """
    app = QtWidgets.QApplication.instance()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.MetaCall)
    app.processEvents()
    loop = QtCore.QEventLoop()
    deadline = QtCore.QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    probe = QtCore.QTimer()
    probe.setInterval(10)
    completed = False
    stopped_passes = 0
    started = time.monotonic()

    def check():
        nonlocal completed, stopped_passes
        if any(state['state'] != 'Stopped' for state in animation_states(roots)):
            stopped_passes = 0
            return
        # Repaint is synchronous. Any style/theme MetaCalls it triggers get a
        # further event-loop turn before a second stopped-state confirmation.
        for root in roots:
            root.ensurePolished()
            root.repaint()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.UpdateRequest)
        stopped_passes += 1
        if stopped_passes >= 2:
            completed = True
            loop.quit()

    probe.timeout.connect(check)
    deadline.start(timeout_ms)
    probe.start()
    loop.exec()
    probe.stop()
    deadline.stop()
    if not completed:
        evidence = Path(__file__).resolve().parents[2] / 'test-results/theme-wait-timeout'
        evidence.mkdir(parents=True, exist_ok=True)
        for index, root in enumerate(roots):
            root.grab().save(str(evidence / f'window-{index}.png'))
        states = {'elapsed_seconds': time.monotonic() - started,
                  'animations': animation_states(roots)}
        (evidence / 'states.json').write_text(json.dumps(states, indent=2))
        raise AssertionError('Qt animations did not settle: ' + json.dumps(states))
