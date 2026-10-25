"""Import-order coverage without loading host Qt, Paddle or VC libraries."""
import builtins
from types import SimpleNamespace

import pytest

from scripts import check_runtime


@pytest.mark.parametrize('system', ['win32', 'linux', 'darwin'])
@pytest.mark.parametrize('backend', ['paddle', 'native'])
def test_paddle_diagnostic_initializes_bundled_windows_qt_first(monkeypatch, system, backend):
    imports = []
    modules = {name: SimpleNamespace(name=name) for name in ('PySide6', 'paddle', 'paddleocr')}
    original_import = builtins.__import__

    def record_import(name, *args, **kwargs):
        if name in modules:
            imports.append(name)
            return modules[name]
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(check_runtime, 'sys', SimpleNamespace(platform=system))
    monkeypatch.setattr(builtins, '__import__', record_import)
    result = check_runtime.import_paddle_runtime(backend)
    if backend == 'native':
        assert result == (None, None)
        assert imports == []
    else:
        assert result == (modules['paddle'], modules['paddleocr'])
        assert imports == (['PySide6'] if system == 'win32' else []) + ['paddle', 'paddleocr']


def test_windows_paddle_diagnostic_does_not_fall_back_after_qt_failure(monkeypatch):
    imports = []
    original_import = builtins.__import__

    def fail_qt(name, *args, **kwargs):
        if name in ('PySide6', 'paddle', 'paddleocr'):
            imports.append(name)
            raise ImportError('bundled Qt initialization failed')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(check_runtime, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(builtins, '__import__', fail_qt)
    with pytest.raises(ImportError, match='bundled Qt initialization failed'):
        check_runtime.import_paddle_runtime('paddle')
    assert imports == ['PySide6']
